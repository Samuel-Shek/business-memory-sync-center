#!/usr/bin/env python3
"""Read-only status aggregation for the local BUSINESS memory sync dashboard.

This module deliberately reads only scheduler metadata, redacted receipts,
aggregate ledgers and lock ownership.  It never opens message bodies, chat
content, attachments, Keychain data or raw WeChat databases.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import errno
import fcntl
import hashlib
import json
import os
import plistlib
import re
import sqlite3
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote

import business_sync_state as syncstate
import validation_evidence


ROOT = Path(__file__).resolve().parents[1]
LOCAL_TZ = datetime.now().astimezone().tzinfo
PIPELINE_ORDER = ("dingtalk", "email", "win11", "wechat", "chatgpt", "core")
PIPELINE_NAMES = {
    "dingtalk": "钉钉聊天与听记",
    "email": "企业邮件",
    "win11": "Windows 11 记忆",
    "wechat": "微信",
    "chatgpt": "ChatGPT 项目摘要",
    "core": "记忆库核心",
}
SOURCE_ORDER = (
    "dingtalk_chat",
    "dingtalk_minutes",
    "dingtalk_mail",
    "win11",
    "wechat",
)
SOURCE_NAMES = {
    "dingtalk_chat": "钉钉聊天",
    "dingtalk_minutes": "钉钉 AI 听记",
    "dingtalk_mail": "钉钉邮件",
    "win11": "Win11",
    "wechat": "微信",
}
SOURCE_ACTION_TYPES = {
    "dingtalk_chat": {"dingtalk_chat", "sync_now", "all"},
    "dingtalk_minutes": {"dingtalk_minutes", "sync_now", "all"},
    "dingtalk_mail": {"dingtalk_mail", "all"},
    "win11": {"win11", "all"},
    "wechat": {"wechat", "all"},
}
SOURCE_PIPELINES = {
    "dingtalk_chat": "dingtalk", "dingtalk_minutes": "dingtalk",
    "dingtalk_mail": "email", "win11": "win11", "system": "core",
}

MANAGED_SOURCE_IDS = {"dingtalk_chat", "dingtalk_minutes", "dingtalk_mail", "win11"}
ATTENTION_STATUS_CODES = {
    "failed", "partial", "stale", "unknown", "retrying",
    "persistent_failure", "manual_required", "waiting_source",
}
ABNORMAL_SOURCE_STATUS_CODES = {
    "failed", "partial", "stale", "unknown", "retrying", "persistent_failure",
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class DashboardPaths:
    root: Path
    state_root: Path
    external_root: Path
    launch_agents_root: Path
    automations_root: Path
    email_database: Path
    email_safety_receipt: Path
    email_runtime_error: Path
    win11_database: Path
    win11_automation_log: Path
    wechat_status: Path
    chatgpt_ledger: Path
    chatgpt_automation: Path
    win11_automation: Path
    wechat_automation: Path
    dingtalk_chat_status: Path
    dingtalk_chat_incremental_status: Path
    dingtalk_automation_status: Path
    minutes_sync_state: Path
    minutes_inventory: Path
    bridge_database: Path
    folder_watcher_health: Path
    win11_health: Path
    receiver_health: Path
    maintenance_state: Path
    validation_failure: Path
    validation_success: Path
    supervisor_database: Path

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        root: Path = ROOT,
        home: Optional[Path] = None,
    ) -> "DashboardPaths":
        user_home = home or Path.home()
        external = Path(str(config.get("external_root") or (root.parent / "BUSINESS-NonObsidian.nosync")))
        email_section = config.get("email_sync") if isinstance(config.get("email_sync"), dict) else {}
        email_root = Path(str(email_section.get("state_root") or (external / "email-sync")))
        minutes_root = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync'))
        return cls(
            root=root,
            state_root=root / "state",
            external_root=external,
            launch_agents_root=user_home / "Library" / "LaunchAgents",
            automations_root=user_home / ".codex" / "automations",
            email_database=email_root / "state" / "email-sync.sqlite",
            email_safety_receipt=Path(str(
                email_section.get("read_safety_receipt")
                or (email_root / "read-safety-server-receipt.json")
            )),
            email_runtime_error=email_root / "state" / "email-sync-last-runtime-error.json",
            win11_database=Path(str(
                config.get("semantic_database")
                or (external / "machine-ledgers" / "win11-semantic-merge.sqlite")
            )),
            win11_automation_log=root / "logs" / "win11_memory_automation.jsonl",
            wechat_status=external / "wechat-chat" / "state" / "status.json",
            chatgpt_ledger=external / "machine-ledgers" / "对话同步台账.json",
            chatgpt_automation=user_home / ".codex" / "automations" / "chatgpt" / "automation.toml",
            win11_automation=user_home / ".codex" / "automations" / "win11" / "automation.toml",
            wechat_automation=user_home / ".codex" / "automations" / "automation-2" / "automation.toml",
            dingtalk_chat_status=external / "dingtalk-chat" / "state" / "status.json",
            dingtalk_chat_incremental_status=external / "dingtalk-chat" / "state" / "incremental_status.json",
            dingtalk_automation_status=external / "dingtalk-chat" / "state" / "automation_status.json",
            minutes_sync_state=minutes_root / ".state" / "sync-state.json",
            minutes_inventory=minutes_root / ".state" / "remote-inventory.json",
            bridge_database=root / "state" / "bridge.sqlite",
            folder_watcher_health=root / "state" / "folder_watcher_health.json",
            win11_health=root / "state" / "win11_memory_health.json",
            receiver_health=root / "state" / "health.json",
            maintenance_state=root / "state" / "maintenance-mode.json",
            validation_failure=root / "state" / "last-memory-validation-failure.json",
            validation_success=root / "state" / "last-memory-validation-success.json",
            supervisor_database=external / "dingtalk-sync-v2" / "state" / "sync.sqlite",
        )


def default_runner(arguments: list[str], timeout: float = 0.7) -> CommandResult:
    try:
        completed = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr, False)
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", "", True)
    except OSError as exc:
        return CommandResult(127, "", type(exc).__name__, False)


def now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now().astimezone()).isoformat(timespec="seconds")


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?", text):
        text = text.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone()


def newest_time(*values: Any) -> Optional[str]:
    parsed = [(parse_time(value), str(value)) for value in values if value]
    valid = [item for item in parsed if item[0] is not None]
    if not valid:
        return None
    return max(valid, key=lambda item: item[0])[0].isoformat(timespec="seconds")


def later_than(left: Any, right: Any) -> bool:
    left_time = parse_time(left)
    right_time = parse_time(right)
    return bool(left_time and (not right_time or left_time > right_time))


def future_iso(value: Any, seconds: int) -> Optional[str]:
    parsed = parse_time(value)
    return (parsed + timedelta(seconds=seconds)).isoformat(timespec="seconds") if parsed else None


def state_value(code: str, label: str, detail: str = "") -> dict[str, str]:
    return {"code": code, "label": label, "detail": detail}


def evidence(source: str, status: str, observed_at: Any, detail: str = "") -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "observed_at": str(observed_at or ""),
        "detail": detail,
    }


def safe_file_time(path: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=LOCAL_TZ).isoformat(timespec="seconds")
    except OSError:
        return None


def read_json(path: Path, max_bytes: int = 4 * 1024 * 1024) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    try:
        stat = path.stat()
        if stat.st_size > max_bytes:
            return None, "too_large"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid"
    return (payload, None) if isinstance(payload, dict) else (None, "invalid")


def validation_failure_code(payload: dict[str, Any] | None) -> str:
    """Preserve actionable root causes without exposing validation prose."""
    value = payload if isinstance(payload, dict) else {}
    errors = value.get("errors") if isinstance(value.get("errors"), list) else []
    lowered = "\n".join(str(item).lower() for item in errors)
    claim_code = re.search(r"\b(claim_base_[a-z0-9_]{1,64})\b", lowered)
    if claim_code:
        return claim_code.group(1)
    if "claim-base hash mismatch" in lowered or "claim-base hash does not match" in lowered:
        return "claim_base_mismatch"
    return str(value.get("error_code") or "validation_failed")


def read_jsonl_tail(
    path: Path,
    max_bytes: int = 256 * 1024,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Read a bounded tail of a redacted JSONL receipt without following it."""
    try:
        size = path.stat().st_size
        offset = max(0, size - max_bytes)
        with path.open("rb") as handle:
            handle.seek(offset)
            payload = handle.read(max_bytes)
        lines = payload.decode("utf-8").splitlines()
        if offset and lines:
            lines = lines[1:]
        rows: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(row, dict):
                rows.append(row)
                if len(rows) >= limit:
                    break
        return rows, None
    except FileNotFoundError:
        return [], "missing"
    except (OSError, UnicodeDecodeError):
        return [], "invalid"


def readonly_connection(path: Path, timeout: float = 5.0) -> sqlite3.Connection:
    # 5s, not 0.25s: ledger write waits run ~3s at the median and past 20s at the
    # tail, so a quarter second abandoned the lock while the writer was still
    # queued, and the dashboard reported panels as unavailable that were fine.
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
    return connection


def read_supervisor_snapshot(
    path: Path,
    *,
    maintenance_active: bool = False,
    now: datetime | None = None,
) -> tuple[dict[str, Any], Optional[str]]:
    """Read only the supervisor's redacted control tables."""
    if not path.is_file() or path.is_symlink():
        return {}, "supervisor_state_missing"
    try:
        connection = readonly_connection(path)
        try:
            runtime_row = connection.execute(
                "SELECT * FROM supervisor_runtime WHERE runtime_id='singleton'"
            ).fetchone()
            source_rows = connection.execute("SELECT * FROM supervisor_sources").fetchall()
            alert_rows = connection.execute(
                "SELECT * FROM sync_alerts ORDER BY last_seen DESC LIMIT 200"
            ).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return {}, "supervisor_state_unavailable"
    runtime = dict(runtime_row) if runtime_row else {}
    for key in ("on_ac", "idle_ready"):
        if key in runtime:
            runtime[key] = bool(runtime[key])
    sources = {str(row["source_id"]): dict(row) for row in source_rows}
    current = now or datetime.now().astimezone()
    maintenance_exit = parse_time(runtime.get("last_maintenance_exit_at"))
    post_maintenance_pending = bool(
        not maintenance_active
        and maintenance_exit
        and current >= maintenance_exit
        and current - maintenance_exit <= timedelta(minutes=2)
    )
    return {
        "runtime": runtime,
        "sources": sources,
        "alerts": [
            syncstate.public_alert(
                dict(row), maintenance_active=maintenance_active,
                post_maintenance_pending=post_maintenance_pending,
                source_state=sources.get(str(row["source_id"])),
            )
            for row in alert_rows
        ],
    }, None


def queue_count(path: Path) -> Optional[int]:
    try:
        return sum(1 for item in path.iterdir() if not item.name.startswith("."))
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def parse_simple_toml(path: Path) -> tuple[dict[str, str], Optional[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, UnicodeDecodeError):
        return {}, "invalid"
    values: dict[str, str] = {}
    for key in ("kind", "name", "status", "rrule"):
        match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*[\"']([^\"']*)[\"']\s*$", text)
        if match:
            values[key] = match.group(1)
    return values, None if values else "invalid"


def rrule_next(rrule: str, now: datetime) -> Optional[str]:
    if not rrule.startswith("RRULE:"):
        return None
    fields: dict[str, str] = {}
    for part in rrule.removeprefix("RRULE:").split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value
    if fields.get("FREQ") != "DAILY":
        return None
    try:
        hour = int(fields.get("BYHOUR", "0"))
        minute = int(fields.get("BYMINUTE", "0"))
    except ValueError:
        return None
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat(timespec="seconds")


def calendar_next(schedule: Any, now: datetime) -> Optional[str]:
    entries = schedule if isinstance(schedule, list) else [schedule]
    candidates: list[datetime] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            minute = int(entry.get("Minute", 0))
            if "Hour" in entry:
                candidate = now.replace(
                    hour=int(entry["Hour"]), minute=minute, second=0, microsecond=0
                )
                if candidate <= now:
                    candidate += timedelta(days=1)
            else:
                candidate = now.replace(minute=minute, second=0, microsecond=0)
                if candidate <= now:
                    candidate += timedelta(hours=1)
            candidates.append(candidate)
        except (TypeError, ValueError):
            continue
    return min(candidates).isoformat(timespec="seconds") if candidates else None


def schedule_from_plist(path: Path, now: datetime) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (FileNotFoundError, OSError, plistlib.InvalidFileException):
        return {"summary": "计划未知", "next_run": None, "error": "plist_unavailable"}
    if payload.get("KeepAlive"):
        return {"summary": "持续运行", "next_run": None, "error": None}
    if payload.get("StartCalendarInterval") is not None:
        schedule = payload["StartCalendarInterval"]
        next_run = calendar_next(schedule, now)
        return {"summary": "按日历计划", "next_run": next_run, "error": None if next_run else "schedule_unsupported"}
    if payload.get("StartInterval") is not None:
        try:
            seconds = int(payload["StartInterval"])
            return {
                "summary": f"每 {max(1, seconds // 60)} 分钟",
                "next_run": (now + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
                "error": None,
            }
        except (TypeError, ValueError):
            pass
    if payload.get("RunAtLoad"):
        return {"summary": "登录时运行", "next_run": None, "error": None}
    return {"summary": "无自动计划", "next_run": None, "error": None}


class LaunchdReader:
    def __init__(
        self,
        paths: DashboardPaths,
        runner: Callable[[list[str], float], CommandResult],
        now: datetime,
    ):
        self.paths = paths
        self.runner = runner
        self.now = now
        self.uid = os.getuid()
        disabled = runner(["/bin/launchctl", "print-disabled", f"gui/{self.uid}"], 0.8)
        self.disabled_known = disabled.returncode == 0 and not disabled.timed_out
        self.disabled: dict[str, bool] = {}
        if self.disabled_known:
            for label, value in re.findall(r'"([^"]+)"\s*=>\s*(enabled|disabled|true|false)', disabled.stdout):
                self.disabled[label] = value in {"disabled", "true"}

    def read_one(self, label: str) -> dict[str, Any]:
        plist = self.paths.launch_agents_root / f"{label}.plist"
        schedule = schedule_from_plist(plist, self.now)
        result = self.runner(["/bin/launchctl", "print", f"gui/{self.uid}/{label}"], 0.7)
        loaded = result.returncode == 0 and not result.timed_out
        output = result.stdout if loaded else ""
        state_match = re.search(r"(?m)^\s*state = ([^\n]+)", output)
        exit_match = re.search(r"(?m)^\s*last exit code = (-?\d+)", output)
        runs_match = re.search(r"(?m)^\s*runs = (\d+)", output)
        enabled: Optional[bool]
        if self.disabled_known:
            enabled = not self.disabled.get(label, False)
        else:
            enabled = None
        return {
            "label": label,
            "enabled": enabled,
            "loaded": loaded,
            "running": bool(state_match and state_match.group(1).strip() == "running"),
            "state": state_match.group(1).strip() if state_match else ("not_loaded" if not loaded else "unknown"),
            "last_exit_code": int(exit_match.group(1)) if exit_match else None,
            "runs": int(runs_match.group(1)) if runs_match else None,
            "next_run": schedule["next_run"],
            "schedule": schedule["summary"],
            "evidence_status": "timeout" if result.timed_out else ("ok" if loaded or plist.is_file() else "missing"),
        }

    def read_many(self, labels: Iterable[str]) -> dict[str, dict[str, Any]]:
        unique = sorted(set(labels))
        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(unique)))) as pool:
            futures = {pool.submit(self.read_one, label): label for label in unique}
            for future in as_completed(futures):
                label = futures[future]
                try:
                    results[label] = future.result()
                except Exception:
                    results[label] = {
                        "label": label,
                        "enabled": None,
                        "loaded": False,
                        "running": False,
                        "state": "unknown",
                        "last_exit_code": None,
                        "runs": None,
                        "next_run": None,
                        "schedule": "计划未知",
                        "evidence_status": "error",
                    }
        return results


LOCK_DEFINITIONS = (
    ("sync_control", "统一同步控制锁", "sync-center-control.lock", "core", "run", ("business_sync_controller",)),
    ("core_index", "核心写入锁", "memory-index.lock", "core", "write", ("refresh_memory_indexes", "validate_memory", "win11_semantic_merge", "email_sync", "dingtalk", "receiver.py", "business-memory-receiver")),
    ("receiver", "核心接收器锁", "receiver.lock", "core", "service", ("receiver.py", "business-memory-receiver")),
    ("folder_watcher", "Win11 watcher 锁", "folder_watcher.lock", "win11", "service", ("folder_watcher.py", "business-win11-watcher")),
    ("dingtalk_listener", "钉钉监听器锁", "dingtalk_memory_listener.lock", "dingtalk", "service", ("dingtalk_memory_listener", "dingtalk_memory_automation")),
    ("dingtalk_hourly", "钉钉记忆锁", "dingtalk_memory_hourly.lock", "dingtalk", "run", ("dingtalk_sync_orchestrator", "dingtalk_memory_automation")),
    ("dingtalk_chat", "钉钉聊天锁", "dingtalk_chat_sync.lock", "dingtalk", "run", ("sync_dingtalk_chat",)),
    ("dingtalk_ai", "钉钉 AI 槽位", "dingtalk-shared-ai.lock", "dingtalk", "run", ("codex", "dingtalk", "win11")),
    ("email", "企业邮件锁", "email-sync.lock", "email", "run", ("email_sync.py",)),
    ("win11_merge", "Win11 合并锁", "win11_memory_merge.lock", "win11", "run", ("win11_memory_automation", "win11_semantic_merge")),
    ("wechat_collect", "微信采集锁", "wechat_chat_collect.lock", "wechat", "run", ("wechat", "collect_wechat_chat")),
    ("wechat_ai", "微信 AI 槽位", "wechat_ai_queue.lock", "wechat", "run", ("wechat", "codex")),
)


def lock_owner(
    path: Path,
    runner: Callable[[list[str], float], CommandResult],
    expected_markers: tuple[str, ...],
) -> tuple[Optional[int], str, bool]:
    result = runner(["/usr/sbin/lsof", "-t", "--", str(path)], 0.5)
    if result.returncode != 0 or result.timed_out:
        return None, "未知进程", False
    pids = [int(value) for value in re.findall(r"(?m)^\s*(\d+)\s*$", result.stdout)]
    if not pids:
        return None, "未知进程", False
    pid = pids[0]
    process = runner(["/bin/ps", "-p", str(pid), "-o", "command="], 0.4)
    command = process.stdout.strip() if process.returncode == 0 else ""
    known = any(marker.lower() in command.lower() for marker in expected_markers)
    if known:
        label = next((marker for marker in expected_markers if marker.lower() in command.lower()), "已知服务")
        safe_name = Path(label).name.replace(".py", "").replace(".rb", "")
    else:
        safe_name = "未知进程"
    return pid, safe_name, known


def probe_lock(
    path: Path,
    runner: Callable[[list[str], float], CommandResult] = default_runner,
    role: str = "run",
    expected_markers: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "held": False,
            "status": "idle",
            "label": "空闲",
            "pid": None,
            "process": None,
        }
    try:
        descriptor = os.open(str(path), os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return {
            "exists": True,
            "held": None,
            "status": "unknown",
            "label": "未知",
            "pid": None,
            "process": None,
        }
    held = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                held = True
            else:
                return {
                    "exists": True,
                    "held": None,
                    "status": "unknown",
                    "label": "未知",
                    "pid": None,
                    "process": None,
                }
    finally:
        os.close(descriptor)
    if not held:
        return {
            "exists": True,
            "held": False,
            "status": "idle",
            "label": "空闲",
            "pid": None,
            "process": None,
        }
    pid, process, known = lock_owner(path, runner, expected_markers)
    if role == "service" and known:
        status, label = "service_owned", "服务正常占用"
    elif role == "write" and known:
        status, label = "writing", "正在写入"
    elif role == "run" and known:
        status, label = "syncing", "正在同步"
    else:
        status, label = "unknown", "未知进程占用"
    return {
        "exists": True,
        "held": True,
        "status": status,
        "label": label,
        "pid": pid,
        "process": process,
    }


def lock_inventory(
    paths: DashboardPaths,
    runner: Callable[[list[str], float], CommandResult],
) -> list[dict[str, Any]]:
    locks: list[dict[str, Any]] = []
    for lock_id, name, filename, pipeline, role, markers in LOCK_DEFINITIONS:
        item = probe_lock(paths.state_root / filename, runner, role, markers)
        locks.append({
            "id": lock_id,
            "name": name,
            "pipeline": pipeline,
            "role": role,
            **item,
        })
    return locks


def service_component(name: str, service: dict[str, Any]) -> dict[str, Any]:
    enabled = service.get("enabled")
    if enabled is False:
        lifecycle = state_value("disabled", "已停用", "launchd 当前明确停用")
    elif enabled is None:
        lifecycle = state_value("unknown", "未知", "无法确认 launchd 启用状态")
    elif service.get("loaded"):
        lifecycle = state_value("loaded", "已加载", service.get("schedule") or "")
    else:
        lifecycle = state_value("enabled", "已启用未加载", service.get("schedule") or "")
    activity = state_value(
        "running" if service.get("running") else "idle",
        "运行中" if service.get("running") else "空闲",
    )
    exit_code = service.get("last_exit_code")
    health = (
        state_value("failed", "上次退出异常", f"退出码 {exit_code}")
        if enabled is not False
        and isinstance(exit_code, int)
        and exit_code != 0
        and not service.get("running")
        else state_value(
            "healthy",
            "按计划停用" if enabled is False else "正常",
            "旧独立定时器当前明确停用" if enabled is False else "",
        )
    )
    return {
        "name": name,
        "scheduler_owner": "legacy",
        "lifecycle": lifecycle,
        "activity": activity,
        "health": health,
        "last_exit_code": exit_code,
        "next_run": None if enabled is False else service.get("next_run"),
        "schedule": service.get("schedule"),
    }


def base_pipeline(pipeline_id: str) -> dict[str, Any]:
    return {
        "id": pipeline_id,
        "name": PIPELINE_NAMES[pipeline_id],
        "source": PIPELINE_NAMES[pipeline_id] if pipeline_id != "core" else "各来源",
        "target": "示例企业业务记忆库",
        "lifecycle": state_value("unknown", "未知"),
        "activity": state_value("unknown", "未知"),
        "health": state_value("unknown", "未知"),
        "acceptance": state_value("unknown", "未知"),
        "last_attempt": None,
        "last_success": None,
        "last_failure": None,
        "next_run": None,
        "legacy_next_run": None,
        "next_action": None,
        "fresh_until": None,
        "components": [],
        "evidence": [],
        "attention": 0,
        "recovered": False,
        "scheduler_owner": "legacy",
        "legacy_scheduler": state_value("unknown", "旧独立定时器未知"),
    }


def event_id(pipeline: str, kind: str, at: Any) -> str:
    raw = f"{pipeline}|{kind}|{at or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def make_event(
    pipeline: str,
    kind: str,
    status: str,
    title: str,
    detail: str,
    at: Any,
) -> dict[str, Any]:
    return {
        "id": event_id(pipeline, kind, at),
        "pipeline": pipeline,
        "kind": kind,
        "status": status,
        "title": title,
        "detail": detail,
        "time": str(at or ""),
    }


def fold_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold only consecutive, semantically identical records; keep failure history intact."""
    folded: list[dict[str, Any]] = []
    identity_keys = ("pipeline", "kind", "status", "title", "detail")
    for event in events:
        if folded and all(folded[-1].get(key) == event.get(key) for key in identity_keys):
            folded[-1]["repeat_count"] = int(folded[-1].get("repeat_count") or 1) + 1
            folded[-1]["first_time"] = event.get("time") or folded[-1].get("first_time")
            continue
        item = json.loads(json.dumps(event, ensure_ascii=False))
        item["repeat_count"] = 1
        item["first_time"] = item.get("time")
        item["last_time"] = item.get("time")
        folded.append(item)
    return folded


def active_lock(locks: list[dict[str, Any]], pipeline: str, statuses: set[str]) -> bool:
    return any(lock["pipeline"] == pipeline and lock["status"] in statuses for lock in locks)


def earliest_next(services: Iterable[dict[str, Any]]) -> Optional[str]:
    values = [service.get("next_run") for service in services if service.get("next_run")]
    parsed = [(parse_time(value), value) for value in values]
    valid = [item for item in parsed if item[0]]
    return min(valid, key=lambda item: item[0])[1] if valid else None


def legacy_scheduler_state(services: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Describe only the selected old timers, never continuous listeners."""
    rows = list(services)
    enabled = [service.get("enabled") for service in rows]
    if any(value is True for value in enabled):
        return state_value("enabled", "旧独立定时器仍启用")
    if enabled and all(value is False for value in enabled):
        return state_value("disabled", "旧独立定时器已停用")
    return state_value("unknown", "旧独立定时器未知", "部分 launchd 状态无法确认")


def enabled_legacy_next(services: Iterable[dict[str, Any]]) -> Optional[str]:
    """A disabled old timer has no executable next run, even if its plist has a calendar."""
    return earliest_next(service for service in services if service.get("enabled") is True)


def source_legacy_scheduler(
    components: Iterable[dict[str, Any]],
    scheduled_ids: set[str],
) -> tuple[dict[str, str], Optional[str]]:
    timers = [component for component in components if str(component.get("id") or "") in scheduled_ids]
    lifecycle_codes = [str((component.get("lifecycle") or {}).get("code") or "unknown") for component in timers]
    if any(code in {"enabled", "loaded"} for code in lifecycle_codes):
        status = state_value("enabled", "旧独立定时器仍启用")
    elif lifecycle_codes and all(code in {"disabled", "paused"} for code in lifecycle_codes):
        status = state_value("disabled", "旧独立定时器已停用")
    else:
        status = state_value("unknown", "旧独立定时器未知", "部分 launchd 状态无法确认")
    next_run = earliest_next(
        component
        for component in timers
        if str((component.get("lifecycle") or {}).get("code") or "") in {"enabled", "loaded"}
    )
    return status, next_run


def adapt_dingtalk(
    paths: DashboardPaths,
    launchd: dict[str, dict[str, Any]],
    locks: list[dict[str, Any]],
    snapshot: Optional[dict[str, Any]],
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pipeline = base_pipeline("dingtalk")
    pipeline["scheduler_owner"] = "unified_supervisor"
    labels = {
        "listener": ("消息监听", "com.example.business-dingtalk-memory-listener"),
        "chat": ("聊天同步", "com.example.business-dingtalk-chat-sync"),
        "memory": ("记忆同步", "com.example.business-dingtalk-memory-hourly"),
        "minutes": ("听记同步", "com.example.dingtalk-minutes-sync"),
        "minutes_source": ("听记来源整理", "com.example.dingtalk-minutes-memory-source"),
        "minutes_semantic": ("听记语义整理", "com.example.dingtalk-minutes-memory-semantic"),
        "review": ("听记二查", "com.example.dingtalk-minutes-review"),
    }
    services = [launchd[label] for _key, (_name, label) in labels.items()]
    timer_services = [
        launchd[labels[key][1]]
        for key in ("chat", "memory", "minutes", "minutes_source", "minutes_semantic")
    ]
    pipeline["components"] = [
        {"id": key, **service_component(name, launchd[label])}
        for key, (name, label) in labels.items()
    ]
    pipeline["legacy_scheduler"] = legacy_scheduler_state(timer_services)
    pipeline["lifecycle"] = state_value(
        "enabled", "统一监督已接管", "来源发现、条件判断和重试由统一监督器负责"
    )

    chat, chat_error = read_json(paths.dingtalk_chat_incremental_status)
    full_chat, full_chat_error = read_json(paths.dingtalk_chat_status)
    automation, automation_error = read_json(paths.dingtalk_automation_status)
    minutes, minutes_error = read_json(paths.minutes_sync_state)
    inventory, inventory_error = read_json(paths.minutes_inventory)
    sync_types = SOURCE_ACTION_TYPES["dingtalk_chat"] | SOURCE_ACTION_TYPES["dingtalk_minutes"]
    active_actions = [row for row in ((snapshot or {}).get("active_actions") or [])
                      if isinstance(row, dict) and row.get("action_type") in sync_types]
    is_syncing = bool(active_actions) or active_lock(locks, "dingtalk", {"syncing"})
    pipeline["activity"] = state_value(
        "syncing" if is_syncing else "idle",
        "正在同步" if is_syncing else "正常空闲",
        "手动与自动任务共用同一串行控制入口",
    )

    sync_integrity = ((snapshot or {}).get("sync") or {}).get("database_integrity")
    incidents = (snapshot or {}).get("open_incidents") or []
    incident_times = [
        item.get("updated_at") or item.get("created_at")
        for item in incidents if isinstance(item, dict)
    ]
    service_failures = [
        service for service in services
        if service.get("enabled") is not False
        and isinstance(service.get("last_exit_code"), int)
        and service["last_exit_code"] != 0
        and not service.get("running")
    ]

    attention_counts = (snapshot or {}).get("attention_counts") or {}
    attention = sum(int(value or 0) for value in attention_counts.values())
    decisions = len((snapshot or {}).get("decisions") or [])
    pipeline["attention"] = attention + decisions
    pipeline["acceptance"] = (
        state_value("attention", "有待处理项", f"{attention} 条待处理，{decisions} 条高风险核验")
        if attention or decisions
        else state_value("accepted", "验收正常", "当前没有待处理缺口")
    )

    action_rows = list(((snapshot or {}).get("latest_actions") or {}).values())
    action_attempts = [row.get("started_at") for row in action_rows]
    action_successes = [
        row.get("completed_at") or row.get("started_at")
        for row in action_rows if row.get("status") in {"success", "noop", "partial"}
    ]
    action_failures = [
        row.get("completed_at") or row.get("started_at")
        for row in action_rows if row.get("status") in {"failed", "interrupted"}
    ]
    chat_attempt = (chat or {}).get("started_at") or (full_chat or {}).get("started_at")
    chat_success = (
        (chat or {}).get("completed_at")
        if (chat or {}).get("status") == "success"
        else (full_chat or {}).get("completed_at") if (full_chat or {}).get("status") == "success" else None
    )
    minutes_success = (minutes or {}).get("last_sync_at")
    pipeline["last_attempt"] = newest_time(*action_attempts, chat_attempt, (inventory or {}).get("last_attempt_at"))
    pipeline["last_success"] = newest_time(*action_successes, chat_success, minutes_success)
    pipeline["last_failure"] = newest_time(*action_failures, *incident_times)
    current_incidents = bool(
        incidents
        and (not pipeline["last_success"] or later_than(newest_time(*incident_times), pipeline["last_success"]))
    )
    if sync_integrity not in {None, "ok"} or current_incidents or service_failures:
        pipeline["health"] = state_value("failed", "需要检查", "存在新的开放事件或异常退出")
    elif chat_error and full_chat_error and minutes_error:
        pipeline["health"] = state_value("unknown", "状态未知", "钉钉状态凭证不可用")
    else:
        pipeline["health"] = state_value("healthy", "运行正常")
    if pipeline["last_failure"] and later_than(pipeline["last_success"], pipeline["last_failure"]):
        pipeline["recovered"] = True
        if pipeline["health"]["code"] == "healthy":
            pipeline["health"] = state_value("recovered", "已恢复", "历史失败保留，后续成功已恢复")
    pipeline["legacy_next_run"] = enabled_legacy_next(timer_services)
    pipeline["next_run"] = None
    pipeline["next_action"] = "由统一监督器按来源分别判断执行资格"
    freshness_anchor = newest_time(
        pipeline["last_success"],
        (automation or {}).get("updated_at"),
        (snapshot or {}).get("generated_at"),
    )
    pipeline["fresh_until"] = future_iso(freshness_anchor, 2 * 60 * 60)
    pipeline["evidence"] = [
        evidence("launchd", "ok", now_iso(now), "调度、加载、运行和退出码"),
        evidence("钉钉脱敏状态", "ok" if not (chat_error and full_chat_error) else "unknown", safe_file_time(paths.dingtalk_chat_incremental_status), "聊天聚合状态"),
        evidence("听记同步状态", "ok" if not minutes_error else "unknown", safe_file_time(paths.minutes_sync_state), "只读同步清单"),
    ]
    events: list[dict[str, Any]] = []
    for row in action_rows:
        at = row.get("completed_at") or row.get("started_at")
        status = str(row.get("status") or "unknown")
        events.append(make_event(
            "dingtalk", str(row.get("action_type") or "action"), "failed" if status in {"failed", "interrupted"} else "success",
            "钉钉同步或二查", "失败记录已保留" if status in {"failed", "interrupted"} else "任务已结束",
            at,
        ))
    if chat_success:
        events.append(make_event("dingtalk", "chat_sync", "success", "钉钉聊天同步完成", "仅记录聚合状态", chat_success))
    if minutes_success:
        events.append(make_event("dingtalk", "minutes_sync", "success", "钉钉听记同步完成", "仅记录聚合状态", minutes_success))
    if pipeline["recovered"] and pipeline["last_success"]:
        events.append(make_event("dingtalk", "recovery", "recovered", "钉钉流程已恢复", "历史失败仍保留", pipeline["last_success"]))
    return pipeline, events


def adapt_email(
    paths: DashboardPaths,
    config: dict[str, Any],
    launchd: dict[str, dict[str, Any]],
    locks: list[dict[str, Any]],
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pipeline = base_pipeline("email")
    pipeline["scheduler_owner"] = "unified_supervisor"
    service = launchd["com.example.business-enterprise-email-sync"]
    configured = bool((config.get("email_sync") or {}).get("enabled"))
    pipeline["components"] = [
        {"id": "scheduler", **service_component("邮件调度器", service)},
        {
            "id": "processor",
            "name": "邮件处理器",
            "lifecycle": state_value("configured" if configured else "disabled", "配置已启用" if configured else "配置已停用"),
            "activity": state_value("idle", "空闲"),
            "health": state_value("unknown", "读取中"),
            "last_exit_code": None,
            "next_run": None,
            "schedule": f"配置轮询 {int((config.get('email_sync') or {}).get('poll_minutes') or 30)} 分钟",
        },
    ]
    pipeline["legacy_scheduler"] = legacy_scheduler_state([service])
    pipeline["lifecycle"] = state_value(
        "enabled", "统一监督已接管", "旧定时器状态不再代表邮件自动同步是否启用"
    )
    syncing = active_lock(locks, "email", {"syncing"})
    pipeline["activity"] = state_value("syncing" if syncing else "idle", "正在同步" if syncing else "空闲")
    pipeline["legacy_next_run"] = enabled_legacy_next([service])
    pipeline["next_run"] = None
    pipeline["next_action"] = "由统一监督器按邮件来源资格执行"

    safety, safety_error = read_json(paths.email_safety_receipt)
    runtime_error, runtime_error_code = read_json(paths.email_runtime_error)
    rows: list[sqlite3.Row] = []
    counts: dict[str, int] = {}
    attachment_attention = 0
    video_waiting = 0
    partial_attachments = 0
    technical_attention = 0
    phases: dict[str, int] = {}
    db_error: Optional[str] = None
    try:
        connection = readonly_connection(paths.email_database)
        try:
            rows = list(connection.execute(
                """
                SELECT status,created_at,completed_at,last_error_code,
                       discovered_count,archived_count,memory_applied_count,failed_count
                FROM batches ORDER BY created_at DESC LIMIT 50
                """
            ))
            for row in connection.execute(
                "SELECT archive_state,memory_state,report_state,COUNT(*) AS count FROM messages GROUP BY archive_state,memory_state,report_state"
            ):
                counts[f"{row['archive_state']}|{row['memory_state']}|{row['report_state']}"] = int(row["count"])
            import email_phases
            phases = email_phases.counts(connection)
            import operational_workbench
            pending_attachments = operational_workbench.pending_attachment_rows(connection, paths.supervisor_database)
            video_waiting=sum(bool(a.get('video_waiting')) for a in pending_attachments)
            pending_attachments=[a for a in pending_attachments if not a.get('video_waiting')]
            attachment_attention = len(pending_attachments)
            partial_attachments = sum(item.get("extract_status") == "partial_static" for item in pending_attachments)
            technical_attention = int(connection.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE archive_state='failed'
                   OR (archive_state='archived' AND memory_state!='applied' AND last_error_code IS NOT NULL)
                """
            ).fetchone()[0])
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.Error, OSError) as exc:
        db_error = "busy" if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower() else "unavailable"

    successes = [
        row for row in rows
        if str(row["status"]) in {"reported", "completed", "success", "local_only_complete"}
    ]
    failures = [row for row in rows if str(row["status"]) in {"failed", "report_uncertain"} or int(row["failed_count"] or 0) > 0]
    latest = rows[0] if rows else None
    success = successes[0] if successes else None
    failure = failures[0] if failures else None
    pipeline["last_attempt"] = str(latest["created_at"]) if latest else None
    pipeline["last_success"] = str(success["completed_at"] or success["created_at"]) if success else None
    pipeline["last_failure"] = str(failure["completed_at"] or failure["created_at"]) if failure else (
        (runtime_error or {}).get("recorded_at") or (runtime_error or {}).get("at")
    )
    recovered = bool(pipeline["last_failure"] and later_than(pipeline["last_success"], pipeline["last_failure"]))
    pipeline["recovered"] = recovered

    try:
        safety_file_ok = (
            paths.email_safety_receipt.is_file()
            and not paths.email_safety_receipt.is_symlink()
            and stat.S_IMODE(paths.email_safety_receipt.stat().st_mode) == 0o600
        )
    except OSError:
        safety_file_ok = False
    safety_ok = bool(
        safety_file_ok
        and safety
        and safety.get("server_side_pass")
        and safety.get("cross_device_confirmed")
        and safety.get("verdict") == "pass"
    )
    failure_active = bool(pipeline["last_failure"] and not recovered)
    if db_error:
        pipeline["health"] = state_value("unknown", "台账未知", "只读 SQLite 暂时不可用")
    elif failure_active:
        pipeline["health"] = state_value("failed", "最近运行失败", "错误码已脱敏")
    elif recovered:
        pipeline["health"] = state_value("recovered", "已恢复", "历史失败保留，后续批次已成功")
    elif not safety_ok:
        pipeline["health"] = state_value("unknown", "读取安全待确认")
    else:
        pipeline["health"] = state_value("healthy", "处理器健康")
    pipeline["components"][1]["health"] = pipeline["health"]

    pending = sum(
        count
        for key, count in counts.items()
        if not (
            len(key.split("|")) == 3
            and key.split("|")[0] == "archived"
            and key.split("|")[1] == "applied"
        )
    )
    pipeline["reporting_pending"] = sum(count for key, count in counts.items()
        if key.split("|")[:2] == ["archived", "applied"]
        and key.split("|")[2] not in {"reported", "local_only"})
    pipeline["attention"] = pending + attachment_attention
    pipeline["attention_breakdown"] = {
        "technical_failed": technical_attention if not db_error else None,
        "manual_review": attachment_attention if not db_error else None,
        "video_waiting": video_waiting if not db_error else None,
        "partial_extraction": partial_attachments if not db_error else None,
        "pending": max(0, pending - technical_attention) if not db_error else None,
    }
    if db_error:
        pipeline["acceptance"] = state_value("unknown", "验收未知")
    elif pending:
        pipeline["acceptance"] = state_value("pending", "存在待处理", f"{pending} 封邮件尚未完成归档或记忆处理")
    elif phases.get("pending"):
        pipeline["acceptance"] = state_value("pending", "正文已处理，附件分阶段补录", "图片或附件未完成，不代表整封已处理")
    elif attachment_attention:
        pipeline["acceptance"] = state_value("attention", "邮件已处理，附件待核验",
            f"邮件归档与记忆处理已完成；{attachment_attention} 项附件读取待核验，其中 {partial_attachments} 项为部分提取")
    elif video_waiting:
        pipeline["acceptance"] = state_value("waiting", "其他内容已处理，视频待命", f"{video_waiting} 项附件含视频待命；不代表整份附件完整读取")
    else:
        pipeline["acceptance"] = state_value("accepted", "归档与记忆已应用")
    if not db_error:
        mail_result = f"{pending} 封邮件归档或记忆处理未完成" if pending else "邮件归档与记忆处理已完成"
        pipeline["processing_summary"] = (mail_result + f"；{attachment_attention} 项附件读取待核验"
            + (f"（其中 {partial_attachments} 项为部分提取）" if partial_attachments else ""))
    if (phases.get("total") or phases.get("pending") or phases.get("body_applied")) and not db_error:
        pipeline["phases"] = phases
        pipeline["processing_summary"] = (f"正文已入库 {phases.get('body_applied',0)} 封；图片待处理 {phases['image_pending']} 项，"
            f"附件等待 Win11 {phases['win11_pending']} 项，待补录 {phases['awaiting_supplement']} 项，"
            f"补录完成 {phases['completed']} 项，需人工处理 {phases['needs_review']} 项，图片引用缺口 {phases.get('reference_gaps',0)} 项，历史来源缺口 {phases.get('source_gaps',0)} 封，自己发出的附件按规则跳过 {phases.get('self_skipped',0)} 项"
            + (f"；{pending} 封邮件当前内容尚未入库" if pending else ""))
    if video_waiting and not db_error:
        pipeline['processing_summary'] += f'；{video_waiting} 项附件视频等待指令'
    evidence_time = newest_time(safe_file_time(paths.email_database), safe_file_time(paths.email_safety_receipt), now_iso(now))
    pipeline["fresh_until"] = future_iso(now_iso(now), 90) if service.get("enabled") is False else future_iso(pipeline["last_success"], 2 * 60 * 60)
    pipeline["evidence"] = [
        evidence("launchd", "ok" if service.get("enabled") is not None else "unknown", now_iso(now), "调度器启用、加载和运行状态"),
        evidence("邮件只读台账", "unknown" if db_error else "ok", safe_file_time(paths.email_database), "不读取正文"),
        evidence("读取安全回执", "ok" if safety_ok else ("unknown" if safety_error else "attention"), safe_file_time(paths.email_safety_receipt), "服务器端只读校验"),
    ]
    events: list[dict[str, Any]] = []
    for row in rows[:20]:
        status = str(row["status"])
        failed = status in {"failed", "report_uncertain"} or int(row["failed_count"] or 0) > 0
        at = row["completed_at"] or row["created_at"]
        events.append(make_event(
            "email", "batch", "failed" if failed else "success",
            "企业邮件批次失败" if failed else "企业邮件批次完成",
            f"发现 {int(row['discovered_count'] or 0)} 封；不包含正文",
            at,
        ))
    if recovered and pipeline["last_success"]:
        events.append(make_event("email", "recovery", "recovered", "企业邮件流程已恢复", "历史失败仍保留", pipeline["last_success"]))
    return pipeline, events


def adapt_win11(
    paths: DashboardPaths,
    launchd: dict[str, dict[str, Any]],
    locks: list[dict[str, Any]],
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pipeline = base_pipeline("win11")
    pipeline["scheduler_owner"] = "unified_supervisor"
    hourly = launchd["com.example.business-win11-memory-hourly"]
    watcher = launchd["com.example.business-memory-folder-watcher"]
    pipeline["components"] = [
        {"id": "source_watcher", **service_component("Win11 只读 watcher", watcher)},
        {"id": "hourly_merge", **service_component("Win11 小时合并", hourly)},
    ]
    pipeline["legacy_scheduler"] = legacy_scheduler_state([hourly])
    pipeline["lifecycle"] = state_value(
        "enabled", "统一监督已接管", "watcher 持续监测；合并资格由统一监督器判断"
    )
    syncing = active_lock(locks, "win11", {"syncing"})
    pipeline["activity"] = state_value("syncing" if syncing else "idle", "正在同步" if syncing else "正常空闲")
    pipeline["legacy_next_run"] = enabled_legacy_next([hourly])
    pipeline["next_run"] = None
    pipeline["next_action"] = "由统一监督器按 Win11 来源资格执行"

    watcher_health, watcher_error = read_json(paths.folder_watcher_health)
    merge_health, merge_error = read_json(paths.win11_health)
    automation_rows, automation_error = read_jsonl_tail(paths.win11_automation_log)
    rows: list[sqlite3.Row] = []
    db_error: Optional[str] = None
    try:
        connection = readonly_connection(paths.win11_database)
        try:
            rows = list(connection.execute(
                "SELECT kind,status,created_at,completed_at,writes_count,error_code FROM runs ORDER BY created_at DESC LIMIT 50"
            ))
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.Error, OSError) as exc:
        db_error = "busy" if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower() else "unavailable"
    latest = rows[0] if rows else None
    successes = [row for row in rows if str(row["status"]) in {"completed", "success", "noop"}]
    failures = [row for row in rows if str(row["status"]) in {"failed", "blocked", "interrupted"}]
    success = successes[0] if successes else None
    failure = failures[0] if failures else None
    successful_automation = next(
        (row for row in automation_rows if row.get("event") in {"prepare_noop", "committed", "dry_run_complete"} and row.get("at")),
        None,
    )
    failed_automation = next(
        (row for row in automation_rows if row.get("event") in {"failed", "blocked", "prepared_run_close_failed"} and row.get("at")),
        None,
    )
    latest_automation = next((row for row in automation_rows if row.get("at")), None)
    merge_attempt = str(latest["created_at"]) if latest else (merge_health or {}).get("updated_at")
    merge_success = str(success["completed_at"] or success["created_at"]) if success else None
    merge_failure = str(failure["completed_at"] or failure["created_at"]) if failure else (
        ((watcher_health or {}).get("last_error") or {}).get("at")
    )
    pipeline["last_attempt"] = newest_time(merge_attempt, (latest_automation or {}).get("at"))
    pipeline["last_success"] = newest_time(merge_success, (successful_automation or {}).get("at"))
    pipeline["last_failure"] = newest_time(merge_failure, (failed_automation or {}).get("at"))
    recovered = bool(pipeline["last_failure"] and later_than(pipeline["last_success"], pipeline["last_failure"]))
    pipeline["recovered"] = recovered
    watcher_bad = bool(
        watcher_error
        or (watcher_health or {}).get("status") not in {"running", "idle", "ok"}
        or not watcher.get("running")
    )
    latest_failed = bool(
        pipeline["last_failure"]
        and (not pipeline["last_success"] or later_than(pipeline["last_failure"], pipeline["last_success"]))
    )
    if db_error or merge_error:
        pipeline["health"] = state_value("unknown", "状态未知", "Win11 只读台账或健康文件不可用")
    elif watcher_bad:
        pipeline["health"] = state_value("failed", "watcher 异常", "来源监测没有处于正常运行态")
    elif latest_failed:
        pipeline["health"] = state_value("failed", "最近合并失败", str(failure["error_code"] or "unknown"))
    elif recovered:
        pipeline["health"] = state_value("recovered", "已恢复", "先前失败保留，后续运行已成功")
    else:
        pipeline["health"] = state_value("healthy", "运行正常")
    pending = int((merge_health or {}).get("pending_events") or 0)
    pipeline["attention"] = pending
    if db_error:
        pipeline["acceptance"] = state_value("unknown", "验收未知")
    elif latest_failed:
        pipeline["acceptance"] = state_value("blocked", "尚未验收", "最近合并没有成功结束")
    elif pending:
        pipeline["acceptance"] = state_value("pending", "等待合并", f"{pending} 个事件待处理")
    elif successful_automation and successful_automation.get("event") == "prepare_noop" and later_than(
        successful_automation.get("at"),
        merge_success,
    ):
        pipeline["acceptance"] = state_value("accepted", "已验收", "最近一次检查无新内容；既有合并已完成")
    else:
        pipeline["acceptance"] = state_value("accepted", "已验收", "最近一次合并已完成")
    freshness_anchor = newest_time(
        pipeline["last_success"],
        (watcher_health or {}).get("updated_at"),
        (merge_health or {}).get("updated_at"),
    )
    pipeline["fresh_until"] = future_iso(freshness_anchor, 2 * 60 * 60)
    pipeline["evidence"] = [
        evidence("launchd", "ok" if hourly.get("enabled") is not None else "unknown", now_iso(now), "watcher 与小时调度"),
        evidence("watcher 健康状态", "unknown" if watcher_error else "ok", (watcher_health or {}).get("updated_at"), "只读来源监测"),
        evidence("Win11 语义合并台账", "unknown" if db_error else "ok", safe_file_time(paths.win11_database), "只读 SQLite"),
        evidence("Win11 调度安全日志", "unknown" if automation_error else "ok", safe_file_time(paths.win11_automation_log), "仅解析时间、事件和错误码"),
    ]
    events: list[dict[str, Any]] = []
    for row in rows[:20]:
        failed = str(row["status"]) in {"failed", "blocked", "interrupted"}
        at = row["completed_at"] or row["created_at"]
        detail = str(row["error_code"] or "错误码未知") if failed else f"写入 {int(row['writes_count'] or 0)} 项"
        events.append(make_event(
            "win11", "merge", "failed" if failed else "success",
            "Win11 合并失败" if failed else "Win11 合并完成",
            detail,
            at,
        ))
    if recovered and pipeline["last_success"]:
        events.append(make_event("win11", "recovery", "recovered", "Win11 流程已恢复", "失败记录仍保留", pipeline["last_success"]))
    for row in automation_rows[:20]:
        if row.get("event") == "prepare_noop" and row.get("at"):
            events.append(make_event(
                "win11", "schedule", "success", "Win11 检查完成",
                "无新内容；未读取源正文", row.get("at"),
            ))
    return pipeline, events


def adapt_wechat(
    paths: DashboardPaths,
    launchd: dict[str, dict[str, Any]],
    locks: list[dict[str, Any]],
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pipeline = base_pipeline("wechat")
    pipeline["scheduler_owner"] = "legacy"
    service = launchd["com.example.business-wechat-chat-sync"]
    automation, automation_error = parse_simple_toml(paths.wechat_automation)
    status, status_error = read_json(paths.wechat_status)
    paused = service.get("enabled") is False or automation.get("status") == "PAUSED"
    pipeline["components"] = [
        {"id": "collector", **service_component("微信安全采集器", service)},
        {
            "id": "automation",
            "name": "微信记忆整理",
            "lifecycle": state_value("paused" if automation.get("status") == "PAUSED" else "enabled", "已暂停" if automation.get("status") == "PAUSED" else "已启用"),
            "activity": state_value("idle", "空闲"),
            "health": state_value("healthy" if not automation_error else "unknown", "配置可读" if not automation_error else "配置未知"),
            "last_exit_code": None,
            "next_run": None if automation.get("status") == "PAUSED" else rrule_next(automation.get("rrule", ""), now),
            "schedule": "Codex 自动化",
            "scheduler_owner": "legacy",
        },
    ]
    pipeline["lifecycle"] = (
        state_value("paused", "尚未上线", "已实现，但登记未完成且当前调度保持停用")
        if paused else state_value("enabled", "已启用")
    )
    syncing = active_lock(locks, "wechat", {"syncing"})
    pipeline["activity"] = state_value("syncing" if syncing else "idle", "正在同步" if syncing else "空闲")
    error_code = str((status or {}).get("error_code") or "")
    if status_error:
        pipeline["health"] = state_value("unknown", "安全状态未知")
        pipeline["acceptance"] = state_value("unknown", "登记状态未知")
    elif error_code == "enrollment_required" or (status or {}).get("status") == "enrollment_required":
        pipeline["health"] = state_value("attention", "等待登记", "不会读取原始微信数据库或 Keychain")
        pipeline["acceptance"] = state_value("not_enrolled", "尚未完成登记")
        pipeline["attention"] = 1
    elif (status or {}).get("status") in {"success", "idle", "ok"}:
        pipeline["health"] = state_value("healthy", "状态正常")
        pipeline["acceptance"] = state_value("accepted", "最近安全步骤已完成")
    else:
        pipeline["health"] = state_value("failed", "最近步骤失败", error_code or "unknown")
        pipeline["acceptance"] = state_value("blocked", "尚未验收")
        pipeline["attention"] = 1
    pipeline["last_attempt"] = (status or {}).get("started_at")
    pipeline["last_success"] = (status or {}).get("finished_at") if (status or {}).get("status") == "success" else None
    pipeline["last_failure"] = None if error_code == "enrollment_required" else (
        (status or {}).get("finished_at") if pipeline["health"]["code"] == "failed" else None
    )
    pipeline["next_run"] = None if paused else (
        rrule_next(automation.get("rrule", ""), now) or service.get("next_run")
    )
    pipeline["next_action"] = "完成安全登记后启用" if paused else "按旧独立计划执行"
    pipeline["legacy_scheduler"] = state_value(
        "paused" if paused else "enabled",
        "旧独立定时器已暂停" if paused else "旧独立定时器已启用",
    )
    pipeline["fresh_until"] = future_iso(now_iso(now), 90)
    pipeline["evidence"] = [
        evidence("launchd", "ok" if service.get("enabled") is not None else "unknown", now_iso(now), "当前停用状态保持不变"),
        evidence("微信脱敏安全状态", "unknown" if status_error else "ok", safe_file_time(paths.wechat_status), "不含聊天正文、附件或密钥"),
        evidence("Codex 自动化配置", "unknown" if automation_error else "ok", safe_file_time(paths.wechat_automation), "只读配置"),
    ]
    events: list[dict[str, Any]] = []
    if error_code == "enrollment_required":
        events.append(make_event("wechat", "enrollment", "blocked", "微信同步等待登记", "当前保持暂停，未读取原始数据", (status or {}).get("finished_at")))
    elif pipeline["last_success"]:
        events.append(make_event("wechat", "safe_status", "success", "微信安全步骤完成", "不包含内容数据", pipeline["last_success"]))
    elif pipeline["last_failure"]:
        events.append(make_event("wechat", "safe_status", "failed", "微信安全步骤失败", error_code or "unknown", pipeline["last_failure"]))
    return pipeline, events


def adapt_chatgpt(
    paths: DashboardPaths,
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pipeline = base_pipeline("chatgpt")
    pipeline["scheduler_owner"] = "legacy"
    pipeline["compatibility_only"] = True
    automation, automation_error = parse_simple_toml(paths.chatgpt_automation)
    ledger, ledger_error = read_json(paths.chatgpt_ledger)
    active = automation.get("status") == "ACTIVE"
    pipeline["components"] = [{
        "id": "project_summary",
        "name": "项目摘要同步",
        "lifecycle": state_value("enabled" if active else ("unknown" if automation_error else "paused"), "已启用" if active else ("未知" if automation_error else "已暂停")),
        "activity": state_value("idle", "空闲"),
        "health": state_value("unknown", "读取中"),
        "last_exit_code": None,
        "next_run": rrule_next(automation.get("rrule", ""), now),
        "schedule": "每日项目摘要",
        "scheduler_owner": "legacy",
    }]
    pipeline["lifecycle"] = pipeline["components"][0]["lifecycle"]
    pipeline["activity"] = state_value("idle", "空闲", "只读取指定示例企业项目的聚合台账")
    pipeline["next_run"] = pipeline["components"][0]["next_run"]
    pipeline["next_action"] = "仅在兼容只读页按原计划执行"
    pipeline["legacy_scheduler"] = state_value(
        "enabled" if active else ("unknown" if automation_error else "paused"),
        "旧独立定时器已启用" if active else ("旧独立定时器未知" if automation_error else "旧独立定时器已暂停"),
    )
    last_checked = (ledger or {}).get("last_checked_at")
    last_success = (ledger or {}).get("last_success_at")
    failure = (ledger or {}).get("last_failure") if isinstance((ledger or {}).get("last_failure"), dict) else {}
    last_failure = failure.get("at")
    pipeline["last_attempt"] = last_checked
    pipeline["last_success"] = last_success
    pipeline["last_failure"] = last_failure
    failure_active = bool(last_failure and not later_than(last_success, last_failure))
    if automation_error or ledger_error:
        pipeline["health"] = state_value("unknown", "状态未知", "自动化配置或只读台账不可用")
        pipeline["acceptance"] = state_value("unknown", "验收未知")
    elif failure_active:
        step = str(failure.get("step") or "unknown")
        safe_code = "list_threads_timeout" if step == "list_threads" else f"{step}_blocked"
        pipeline["health"] = state_value("failed", "最近同步失败", safe_code)
        pipeline["acceptance"] = state_value("blocked", "本轮未验收", "旧成功仍保留，本轮失败没有改写摘要")
        pipeline["attention"] = 1
    else:
        recovered = bool(last_failure and later_than(last_success, last_failure))
        pipeline["recovered"] = recovered
        pipeline["health"] = state_value("recovered" if recovered else "healthy", "已恢复" if recovered else "运行正常")
        pipeline["acceptance"] = state_value("accepted", "最近同步已验收")
    pipeline["components"][0]["health"] = pipeline["health"]
    pipeline["fresh_until"] = future_iso(last_checked or last_success, 42 * 60 * 60)
    record_count = len((ledger or {}).get("records") or {}) if isinstance((ledger or {}).get("records"), dict) else 0
    pipeline["evidence"] = [
        evidence("Codex 自动化配置", "unknown" if automation_error else "ok", safe_file_time(paths.chatgpt_automation), "仅指定示例企业项目"),
        evidence("对话同步台账", "unknown" if ledger_error else "ok", last_checked or safe_file_time(paths.chatgpt_ledger), f"{record_count} 条聚合记录；不读取对话正文"),
    ]
    events: list[dict[str, Any]] = []
    if last_success:
        events.append(make_event("chatgpt", "sync", "success", "ChatGPT 项目摘要同步完成", f"{record_count} 条台账记录", last_success))
    if last_failure:
        step = str(failure.get("step") or "unknown")
        detail = "线程列表读取超时" if step == "list_threads" else f"{step} 阶段阻断"
        events.append(make_event("chatgpt", "sync", "failed", "ChatGPT 项目摘要同步失败", detail, last_failure))
    if pipeline["recovered"] and last_success:
        events.append(make_event("chatgpt", "recovery", "recovered", "ChatGPT 项目摘要流程已恢复", "历史失败仍保留", last_success))
    return pipeline, events


def adapt_core(
    paths: DashboardPaths,
    launchd: dict[str, dict[str, Any]],
    locks: list[dict[str, Any]],
    now: datetime,
    *, file_cache=None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Optional[int]]]:
    pipeline = base_pipeline("core")
    receiver = launchd["com.example.business-memory-bridge"]
    watcher = launchd["com.example.business-memory-folder-watcher"]
    pipeline["components"] = [
        {"id": "receiver", **service_component("记忆接收器", receiver)},
        {"id": "index", "name": "索引刷新与校验", "lifecycle": state_value("enabled", "按写入触发"), "activity": state_value("idle", "空闲"), "health": state_value("unknown", "读取中"), "last_exit_code": None, "next_run": None, "schedule": "按写入触发"},
    ]
    maintenance, maintenance_error = read_json(paths.maintenance_state)
    maintenance_active = bool(maintenance_error or (maintenance or {}).get("status") != "inactive")
    if maintenance_active:
        pipeline["lifecycle"] = state_value("maintenance", "维护中", "进程预期停止时不判为故障")
    elif receiver.get("enabled") is False:
        pipeline["lifecycle"] = state_value("disabled", "接收器已停用")
    elif receiver.get("enabled") is None:
        pipeline["lifecycle"] = state_value("unknown", "接收器状态未知")
    else:
        pipeline["lifecycle"] = state_value("enabled", "核心接收已启用")
    writing = active_lock(locks, "core", {"writing"})
    pipeline["activity"] = state_value("writing" if writing else "idle", "正在写入" if writing else "正常空闲")

    receiver_health, receiver_error = read_json(paths.receiver_health)
    folder_health, folder_error = read_json(paths.folder_watcher_health)
    validation, validation_error = read_json(paths.validation_failure)
    validation_success, _validation_success_error = read_json(paths.validation_success)
    automatic_path = paths.state_root / "automatic-memory-validation.json"
    automatic, automatic_error = read_json(automatic_path)
    automatic = automatic or {}
    from automatic_memory_validation import valid_accounting
    automatic_invalid = bool(automatic_path.exists() and (automatic_error or not automatic or not valid_accounting(automatic)))
    validating = automatic.get("status") == "running" and bool(writing)
    if validating:
        pipeline["activity"] = state_value("syncing", "正在完整校验", "只读检查最新资料，完成前不放行同步写入")
    maintenance_rows: dict[str, str] = {}
    recent_source_events: list[sqlite3.Row] = []
    db_error: Optional[str] = None
    try:
        connection = readonly_connection(paths.bridge_database)
        try:
            maintenance_rows = {
                str(row["key"]): str(row["value"])
                for row in connection.execute("SELECT key,value FROM maintenance_state")
            }
            recent_source_events = list(connection.execute(
                "SELECT status,source_id,processed_at FROM source_events ORDER BY processed_at DESC LIMIT 30"
            ))
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.Error, OSError) as exc:
        db_error = "busy" if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower() else "unavailable"
    dirty = maintenance_rows.get("index_dirty_generation")
    applied = maintenance_rows.get("index_applied_generation")
    index_error = maintenance_rows.get("index_last_error")
    index_refreshed = maintenance_rows.get("index_last_refreshed_at")
    index_ok = bool(dirty is not None and dirty == applied and not index_error)
    try:
        index_valid = int(dirty) >= int(applied) >= 0
    except (ValueError, TypeError):
        index_valid = False
    index_failed = bool(index_error or (not db_error and not index_valid))
    receiver_ok = bool(
        not receiver_error
        and (receiver_health or {}).get("status") in {"running", "idle", "ok"}
        and receiver.get("running")
    )
    # "offline" means the watcher process is fine but the Win11 share did not
    # answer in time (slow SMB over ZeroTier). That stops Win11 capture only; it
    # must not mark the whole memory core failed and halt DingTalk and mail
    # (2020-01-02: 121 such flaps in four days, each pausing every source).
    folder_ok = bool(
        not folder_error
        and (folder_health or {}).get("status") in {"running", "idle", "ok", "offline"}
        and watcher.get("running")
    )
    validation_at = (validation or {}).get("recorded_at")
    validation_success_at = (validation_success or {}).get("recorded_at")
    validation_status = (validation_evidence.receipt_status(paths.root, validation_success, dirty, applied)
                         if file_cache is None else validation_evidence.receipt_status(
                             paths.root, validation_success, dirty, applied, file_cache=file_cache))
    validation_current = validation_status["current"]
    validation_permission_denied = validation_status["error_code"] == "validation_permission_denied"
    full_success = bool(
        (validation_success or {}).get("schema_version") == 2
        and (validation_success or {}).get("full_validation") is True
        and (validation_success or {}).get("verdict") == "pass"
        and isinstance((validation_success or {}).get("validation_context"), dict)
        and all(re.fullmatch(r"[0-9a-f]{64}", str(validation_success["validation_context"].get(key) or ""))
                for key in ("code_sha256", "source_snapshot_sha256", "database_snapshot_sha256"))
        and (validation_success or {}).get("index_dirty_generation") is not None
        and str(validation_success.get("index_dirty_generation")) == str(validation_success.get("index_applied_generation"))
        and parse_time(validation_success_at)
    )
    # A genuine later pass supersedes a historical failure.  Subsequent new
    # input invalidates CURRENT acceptance, not the truth of that old recovery.
    old_validation_failure = bool(validation_at and full_success and later_than(validation_success_at, validation_at))
    recovered_at = validation_success_at if validation_current and old_validation_failure else None
    active_validation_failure = bool(validation_at and not old_validation_failure)
    active_validation_code = validation_failure_code(validation) if active_validation_failure else None
    if maintenance_active:
        pipeline["health"] = state_value("maintenance", "维护中", "维护窗口内停机不算故障")
    elif validation_permission_denied:
        pipeline["health"] = state_value("failed", "资料读取权限受阻", "当前后台无法读取完整业务资料；已停止自动修复，需要本人允许目录访问")
    elif automatic_invalid:
        pipeline["health"] = state_value("failed", "自动校验记录异常", "保留原记录并停止重试；不能重置次数后继续")
    elif db_error or receiver_error:
        pipeline["health"] = state_value("unknown", "核心状态未知", "只读台账或健康凭证不可用")
    elif not receiver_ok or not folder_ok:
        pipeline["health"] = state_value("failed", "核心服务异常", "接收器或 watcher 未正常运行")
    elif active_validation_failure or index_failed:
        if automatic.get("status") == "retry_wait" and automatic.get("error_code") == "automatic_validation_timeout":
            pipeline["health"] = state_value("failed", "完整校验超时", f"已尝试 {automatic.get('timeout_count', automatic.get('attempt_count', 0))} / 3 次；仅超时允许有限重试，下次 {automatic.get('next_retry_at') or '待安排'}")
        elif str(active_validation_code or "").startswith("claim_base_"):
            pipeline["health"] = state_value("failed", "业务页面校验基线失效", "受保护页面正文与声明基线不一致；客户事实保持停用，等待受治理恢复和完整复验")
        else:
            pipeline["health"] = state_value("failed", "索引或校验异常", "存在明确索引错误或尚未被后续完整校验通过覆盖的失败")
    elif validating:
        pipeline["health"] = state_value("unknown", "完整校验进行中", "正在核对本次回执与当前资料，尚未释放写入锁")
    elif not index_ok:
        pipeline["health"] = state_value("unknown", "新资料等待索引", "索引提交后将自动完整校验；等待不等于故障或恢复")
    elif not validation_current:
        pipeline["health"] = state_value("unknown", "完整校验进行中" if validating else "完整校验待完成", "新资料或程序变化后自动复验；当前尚未取得匹配的完整回执")
    elif old_validation_failure:
        pipeline["health"] = state_value("recovered", "已恢复", "旧失败保留，当前资料已通过后续完整校验")
        pipeline["recovered"] = True
    else:
        pipeline["health"] = state_value("healthy", "核心正常")
    pipeline["components"][0]["health"] = state_value("healthy", "运行正常") if receiver_ok else pipeline["health"]
    pipeline["components"][1]["health"] = pipeline["health"]
    pipeline["components"][1]["activity"] = state_value("syncing", "正在完整校验") if validating else state_value("idle", "空闲")
    if validation_permission_denied:
        pipeline["acceptance"] = state_value("blocked", "权限受阻，未验收", "资料未被完整读取，不能确认校验或恢复")
        pipeline["attention"] = 1
    elif db_error:
        pipeline["acceptance"] = state_value("unknown", "校验未知")
    elif automatic_invalid or active_validation_failure or index_failed:
        pipeline["acceptance"] = state_value("blocked", "尚未验收", "最近一次校验失败尚未被后续成功覆盖")
        pipeline["attention"] = 1
    elif validating:
        pipeline["acceptance"] = state_value("pending", "完整校验进行中", "本次核对结束并释放写入锁后才能继续同步")
    elif index_ok and validation_current:
        pipeline["acceptance"] = state_value("accepted", "索引与校验通过", f"写入代次 {dirty} = 应用代次 {applied}")
    else:
        pipeline["acceptance"] = state_value("pending", "等待完整校验", "普通更新不新建故障；校验通过前仍不放行写入")
    latest_event = str(recent_source_events[0]["processed_at"]) if recent_source_events else None
    pipeline["last_attempt"] = newest_time(latest_event, index_refreshed, automatic.get("started_at"))
    pipeline["last_success"] = validation_success_at if validation_current else None
    pipeline["validation_current"] = validation_current
    pipeline["validation_error_code"] = (
        "validation_permission_denied" if validation_permission_denied else
        "automatic_validation_state_invalid" if automatic_invalid else
        active_validation_code if active_validation_failure else
        "memory_index_update_failed" if index_failed else validation_status["error_code"]
    )
    pipeline["automatic_validation"] = {
        "status": "blocked" if automatic_invalid else "running" if validating else "verified" if validation_current and not active_validation_failure else automatic.get("status", "pending"),
        "last_result": automatic.get("status"), "run_id": automatic.get("run_id"),
        "attempt_count": automatic.get("attempt_count", 0), "next_retry_at": automatic.get("next_retry_at"),
        "index_generation": automatic.get("index_generation"), "started_at": automatic.get("started_at"),
        "pending_since": automatic.get("pending_since"), "stable_since": automatic.get("stable_since"),
        "defer_count": automatic.get("defer_count", 0), "timeout_count": automatic.get("timeout_count", 0),
        "completed_at": automatic.get("completed_at"), "error_code": "automatic_validation_state_invalid" if automatic_invalid else automatic.get("error_code"),
    }
    if not validation_current and not active_validation_failure and not validating and not automatic_invalid:
        pipeline["automatic_validation"]["status"] = "pending"
    pipeline["last_failure"] = validation_at
    pipeline["fresh_until"] = future_iso(newest_time(
        now_iso(now) if validation_current else None,
        (receiver_health or {}).get("updated_at"),
        (folder_health or {}).get("updated_at"),
        index_refreshed,
    ), 5 * 60)
    pipeline["evidence"] = [
        evidence("launchd", "ok" if receiver.get("enabled") is not None else "unknown", now_iso(now), "接收器与 watcher 运行态"),
        evidence("核心健康状态", "unknown" if receiver_error else "ok", (receiver_health or {}).get("updated_at"), "脱敏接收状态"),
        evidence("索引代次", "ok" if index_ok else "failed" if index_failed else "unknown", index_refreshed, "只读 SQLite，普通待提交不等于损坏"),
        evidence("记忆校验", "failed" if active_validation_failure else ("ok" if validation_current else "unknown"), validation_success_at or validation_at, "完整校验回执绑定当前资料、索引代次与程序"),
        evidence("自动完整校验", "failed" if automatic_invalid else "ok" if validation_current else "unknown", automatic.get("updated_at"), "独立只读校验；仅超时最多三次，状态和通知回执不改变业务快照"),
        evidence("维护状态", "unknown" if maintenance_error else "ok", safe_file_time(paths.maintenance_state), "不提供维护开关"),
    ]
    queues = {
        "inbox": queue_count(paths.root / "inbox"),
        "watch_inbox": queue_count(paths.root / "watch_inbox"),
    }
    events: list[dict[str, Any]] = []
    for row in recent_source_events:
        status = str(row["status"])
        events.append(make_event(
            "core", "receive", "success" if status in {"stored", "processed", "success"} else "failed",
            "记忆库接收事件",
            f"来源 {str(row['source_id'])[:48]} · {status}",
            row["processed_at"],
        ))
    if validation_at:
        events.append(make_event("core", "validation", "failed", "记忆校验失败", "失败记录保留", validation_at))
    if old_validation_failure and recovered_at:
        events.append(make_event("core", "recovery", "recovered", "索引与校验已恢复", "后续验收成功，历史失败保留", recovered_at))
    return pipeline, events, queues


def unknown_pipeline(pipeline_id: str, now: datetime, code: str = "adapter_error") -> dict[str, Any]:
    pipeline = base_pipeline(pipeline_id)
    pipeline["lifecycle"] = state_value("unknown", "未知")
    pipeline["activity"] = state_value("unknown", "未知")
    pipeline["health"] = state_value("unknown", "数据不可用", code)
    pipeline["acceptance"] = state_value("unknown", "验收未知")
    pipeline["fresh_until"] = now_iso(now)
    pipeline["evidence"] = [evidence("状态适配器", "failed", now_iso(now), code)]
    pipeline["attention"] = 1
    return pipeline


def pipeline_is_stale(pipeline: dict[str, Any], now: datetime) -> bool:
    fresh_until = parse_time(pipeline.get("fresh_until"))
    return bool(fresh_until and now > fresh_until)


def display_status(pipeline: dict[str, Any], now: datetime, maintenance_active: bool) -> dict[str, str]:
    if pipeline["health"]["code"] in {"failed", "blocked"} or pipeline["acceptance"]["code"] == "blocked":
        return state_value("failed", "需要处理", pipeline["health"].get("detail") or pipeline["acceptance"].get("detail", ""))
    if maintenance_active and pipeline["id"] == "core":
        return state_value("maintenance", "维护中", pipeline["lifecycle"].get("detail", ""))
    if pipeline["activity"]["code"] in {"syncing", "writing"}:
        return state_value("syncing", pipeline["activity"]["label"], pipeline["activity"].get("detail", ""))
    if pipeline_is_stale(pipeline, now):
        return state_value("stale", "数据过期", "超过该流程的证据新鲜度")
    if pipeline["lifecycle"]["code"] in {"paused", "disabled"}:
        return state_value("paused", pipeline["lifecycle"]["label"], pipeline["lifecycle"].get("detail", ""))
    if pipeline["health"]["code"] == "unknown" or pipeline["lifecycle"]["code"] == "unknown":
        if pipeline["id"] == "core" and pipeline["acceptance"]["code"] == "pending":
            return state_value("unknown", pipeline["acceptance"]["label"], pipeline["health"].get("detail", ""))
        return state_value("unknown", "状态未知", pipeline["health"].get("detail", ""))
    if pipeline["health"]["code"] == "recovered":
        return state_value("recovered", "已恢复", pipeline["health"].get("detail", ""))
    return state_value("healthy", "正常空闲", "")


def overall_status(
    pipelines: list[dict[str, Any]],
    maintenance_active: bool,
    now: datetime,
) -> dict[str, str]:
    failed = [pipeline for pipeline in pipelines if pipeline["display_status"]["code"] == "failed"]
    if failed:
        return state_value("failed", "有流程需要处理", "、".join(item["name"] for item in failed))
    if maintenance_active:
        return state_value("maintenance", "记忆库维护中", "维护窗口内部分进程预期停止")
    syncing = [pipeline for pipeline in pipelines if pipeline["display_status"]["code"] == "syncing"]
    if syncing:
        return state_value("syncing", "正在同步", "、".join(item["name"] for item in syncing))
    stale = [pipeline for pipeline in pipelines if pipeline["display_status"]["code"] in {"stale", "unknown"}]
    if stale:
        return state_value("stale", "有状态未知或过期", "、".join(item["name"] for item in stale))
    paused = [pipeline for pipeline in pipelines if pipeline["display_status"]["code"] == "paused"]
    if paused:
        return state_value("paused", "部分流程保持暂停", "、".join(item["name"] for item in paused))
    recovered = [pipeline for pipeline in pipelines if pipeline["display_status"]["code"] == "recovered"]
    if recovered:
        return state_value("recovered", "流程已恢复", "、".join(item["name"] for item in recovered))
    return state_value("healthy", "全部流程正常空闲", "当前没有同步任务")


def source_display_status(
    source: dict[str, Any],
    now: datetime,
    maintenance_active: bool,
) -> dict[str, str]:
    if source["health"]["code"] in {"failed", "blocked"} or source["acceptance"]["code"] == "blocked":
        return state_value(
            "failed",
            "需要处理",
            source["health"].get("detail") or source["acceptance"].get("detail", ""),
        )
    if maintenance_active:
        return state_value("maintenance", "维护中", "当前只查看状态")
    if source["activity"]["code"] in {"syncing", "writing"}:
        return state_value("syncing", "正在同步", source["activity"].get("detail", ""))
    if source["acceptance"]["code"] == "not_enrolled":
        return state_value("paused", "尚未完成登记", source["acceptance"].get("detail", ""))
    if source["health"]["code"] in {"partial", "attention"} or source["acceptance"]["code"] in {"partial", "attention"}:
        return state_value(
            "partial",
            "部分完成",
            source["acceptance"].get("detail") or source["health"].get("detail", ""),
        )
    if pipeline_is_stale(source, now):
        return state_value("stale", "数据过期", "超过该来源的证据新鲜度")
    if source["lifecycle"]["code"] in {"paused", "disabled"}:
        return state_value("paused", source["lifecycle"]["label"], source["lifecycle"].get("detail", ""))
    if source["health"]["code"] == "unknown" or source["lifecycle"]["code"] == "unknown":
        return state_value("unknown", "状态未知", source["health"].get("detail", ""))
    if source["health"]["code"] == "recovered":
        return state_value("recovered", "已恢复", source["health"].get("detail", ""))
    return state_value("healthy", "正常空闲", "")


def clone_source(pipeline: dict[str, Any], source_id: str) -> dict[str, Any]:
    value = json.loads(json.dumps(pipeline, ensure_ascii=False))
    value.update({
        "id": source_id,
        "name": SOURCE_NAMES[source_id],
        "source": SOURCE_NAMES[source_id],
        "target": "示例企业业务记忆库",
    })
    return value


def filtered_components(pipeline: dict[str, Any], component_ids: set[str]) -> list[dict[str, Any]]:
    return [
        json.loads(json.dumps(component, ensure_ascii=False))
        for component in pipeline.get("components") or []
        if str(component.get("id") or "") in component_ids
    ]


def relevant_actions(
    snapshot: Optional[dict[str, Any]],
    source_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    action_types = SOURCE_ACTION_TYPES[source_id]
    partitioned = isinstance(snapshot, dict) and "latest_manual_actions" in snapshot
    active = [
        row for row in ((snapshot or {}).get("active_actions") or [])
        if isinstance(row, dict) and str(row.get("action_type") or "") in action_types
        and (not partitioned or str(row.get("trigger") or "unknown") == "manual")
    ]
    latest_key = "latest_manual_actions" if partitioned else "latest_actions"
    latest = [
        row for row in (((snapshot or {}).get(latest_key) or {}).values())
        if isinstance(row, dict) and str(row.get("action_type") or "") in action_types
    ]
    latest.sort(
        key=lambda row: parse_time(row.get("started_at")) or datetime.min.replace(tzinfo=LOCAL_TZ),
        reverse=True,
    )
    unknown_types = {
        str(value) for value in ((snapshot or {}).get("unknown_trigger_action_types") or [])
    }
    return active, latest, bool(action_types & unknown_types)


def apply_control_action_state(
    source: dict[str, Any],
    snapshot: Optional[dict[str, Any]],
    now: datetime,
    maintenance_active: bool,
) -> tuple[Optional[str], Optional[str]]:
    active, latest, unknown_history = relevant_actions(snapshot, str(source["id"]))
    running = active[0] if active else None
    source["manual_state"] = state_value("idle", "暂无手动任务")
    if running:
        source["activity"] = state_value("syncing", "正在同步", "由统一同步中心启动")
        source["last_attempt"] = newest_time(source.get("last_attempt"), running.get("started_at"))
        source["manual_state"] = state_value("running", "手动任务运行中")
    direct_types = {str(source["id"])}
    if source["id"] in {"dingtalk_chat", "dingtalk_minutes"}:
        direct_types.add("sync_now")
    direct = next((row for row in latest if str(row.get("action_type") or "") in direct_types), None)
    if direct:
        at = direct.get("completed_at") or direct.get("started_at")
        source["last_attempt"] = newest_time(source.get("last_attempt"), direct.get("started_at"))
        status = str(direct.get("status") or "unknown")
        if direct.get("error_code") == "validation_inputs_changed":
            source["manual_state"] = state_value("deferred", "同步步骤已保留，校验待完成")
        elif status in {"failed", "interrupted"} and (
            not source.get("last_success") or later_than(at, source.get("last_success"))
        ):
            source["last_failure"] = newest_time(source.get("last_failure"), at)
            source["health"] = state_value("failed", "最近手动同步失败", str(direct.get("error_code") or "unknown"))
            source["manual_state"] = state_value(
                "failed", "最近手动任务失败", str(direct.get("error_code") or "unknown")
            )
        elif status in {"success", "noop"}:
            source["last_success"] = newest_time(source.get("last_success"), at)
            source["manual_state"] = state_value(
                "success", "最近手动任务完成" if status == "success" else "最近手动检查无变化"
            )
        elif status == "partial" and not (
            source["id"] == "dingtalk_chat"
            and str(direct.get("action_type") or "") == "sync_now"
            and str(direct.get("error_code") or "") == "recording_media_pending"
        ):
            source["last_success"] = newest_time(source.get("last_success"), at)
            existing_detail = str((source.get("acceptance") or {}).get("detail") or "")
            error_code = str(direct.get("error_code") or "")
            safe_detail = existing_detail or {
                "recording_media_pending": "原始媒体仍待补齐",
            }.get(error_code, "存在待重试项")
            source["acceptance"] = state_value("partial", "部分完成", safe_detail)
            source["manual_state"] = state_value("partial", "最近手动任务部分完成", safe_detail)
        elif status == "partial":
            source["last_success"] = newest_time(source.get("last_success"), at)
            source["manual_state"] = state_value("partial", "最近手动任务部分完成")
        elif not running:
            source["manual_state"] = state_value("unknown", "最近手动任务状态未知")
    elif unknown_history and not running:
        source["manual_state"] = state_value(
            "unknown", "旧任务触发方式未知", "升级前记录未区分手动与自动"
        )
    source["display_status"] = source_display_status(source, now, maintenance_active)
    return (
        str(running.get("run_id")) if running else None,
        str(direct.get("run_id")) if direct else None,
    )


def apply_supervisor_source_state(
    source: dict[str, Any],
    supervisor: dict[str, Any],
    now: datetime,
    maintenance_active: bool,
) -> None:
    runtime = supervisor.get("runtime") if isinstance(supervisor.get("runtime"), dict) else {}
    source_rows = supervisor.get("sources") if isinstance(supervisor.get("sources"), dict) else {}
    row = source_rows.get(str(source["id"])) if isinstance(source_rows, dict) else None
    alerts = [
        alert for alert in (supervisor.get("alerts") or [])
        if isinstance(alert, dict) and str(alert.get("source_id") or "") == str(source["id"])
    ]
    alert_state_priority = {
        "critical": 4,
        "warning": 3,
        "maintenance_pending": 2,
        "recovered": 1,
        "resolved": 0,
    }
    alerts.sort(
        key=lambda alert: (
            int(str(alert.get("status") or "") == "open"),
            alert_state_priority.get(str(alert.get("effective_state") or ""), -1),
            str(alert.get("last_seen") or ""),
        ),
        reverse=True,
    )
    heartbeat = parse_time(runtime.get("heartbeat_at"))
    fresh = bool(heartbeat and now - heartbeat <= timedelta(seconds=150))
    source["automation"] = {
        "managed": source["id"] in MANAGED_SOURCE_IDS,
        "fresh": fresh,
        "last_probe": row.get("last_probe_at") if isinstance(row, dict) else None,
        "supervisor_heartbeat": runtime.get("heartbeat_at"),
        "runtime_status": runtime.get("status"),
        "blocked_reason": runtime.get("blocked_reason"),
        "next_eligible_at": runtime.get("next_eligible_at"),
    }
    latest_alert = alerts[0] if alerts else None
    source["alert"] = {
        key: latest_alert.get(key)
        for key in (
            "alert_id", "category", "severity", "title", "detail", "status",
            "last_seen", "resolved_at", "acknowledged_at", "macos_status",
            "feishu_status", "feishu_receipt_hash", "occurrence_count",
            "effective_state", "delivery", "action_required", "action_label",
        )
    } if latest_alert else None
    if not isinstance(row, dict):
        source["retry"] = {
            "state": "unknown", "attempts": 0, "next_retry": None,
            "last_probe": None, "error_code": "supervisor_state_missing",
        }
        source["automatic_state"] = state_value(
            "unknown", "自动状态未知", "监督器尚未登记该来源状态"
        )
        source["automation_freshness"] = state_value(
            "fresh" if fresh else "stale",
            "自动证据新鲜" if fresh else "自动证据过期",
        )
        return
    source["retry"] = {
        "state": str(row.get("status") or "unknown"),
        "attempts": int(row.get("attempt_count") or row.get("probe_count") or 0),
        "next_retry": row.get("next_due"),
        "last_probe": row.get("last_probe_at"),
        "error_code": row.get("error_code"),
        "blocked_since": row.get("blocked_since"),
        "recovered_at": row.get("recovery_at"),
    }
    breakdown = source.get("attention_breakdown") or {}
    mail_counts_current = (source.get("id") == "dingtalk_mail"
        and row.get("error_code") == "attachment_review_required") and all(
        isinstance(breakdown.get(key), (int, float)) and breakdown[key] >= 0
        for key in ("technical_failed", "manual_review", "pending"))
    if not mail_counts_current:
        source["attention"] = max(int(source.get("attention") or 0), int(row.get("attention_count") or 0))
    planned_at = row.get("next_due") or source.get("next_run")
    status = str(row.get("status") or "pending")
    planned_time = parse_time(planned_at)
    due = bool(planned_time and planned_time <= now)
    blocked_reason = str(runtime.get("blocked_reason") or "") or None
    background_mail = source.get("background_collection_while_active") is True
    if background_mail and blocked_reason == "waiting_idle":
        blocked_reason = None
    source["eligibility"] = {
        "planned_at": planned_at,
        "eligible_at": runtime.get("next_eligible_at"),
        "blocked_reason": blocked_reason,
        "due": due,
    }
    next_actions = {
        "waiting_idle": "已到期，空闲后立即执行" if due else "空闲条件满足后执行",
        "waiting_power": "已到期，接电后立即执行" if due else "接电后执行",
        "deferred": "条件满足后立即执行" if due else "解除阻挡后执行",
        "waiting_source": "上游来源就绪后立即执行",
        "manual_required": "等待人工处理；后台继续探测",
        "persistent_failure": "按退避计划继续探测",
        "retrying": "按重试计划自动执行",
        "running": "正在执行",
    }
    source["next_action"] = next_actions.get(status, "按计划时间执行" if planned_at else "等待监督器计划")
    runtime_status = str(runtime.get("status") or "")
    runtime_blockable_statuses = {
        "healthy", "pending", "retrying", "waiting_idle", "waiting_power", "deferred"
    }
    runtime_condition_blocked = (
        due
        and status in runtime_blockable_statuses
        and runtime_status in {"maintenance", "waiting_power", "waiting_idle"}
        and not (background_mail and runtime_status == "waiting_idle")
    )
    if runtime_condition_blocked:
        source["next_action"] = {
            "maintenance": "维护结束后立即执行",
            "waiting_power": "已到期，接电后立即执行",
            "waiting_idle": "已到期，空闲后立即执行",
        }[runtime_status]
    condition_blocked = status in {"waiting_idle", "waiting_power", "deferred", "manual_required"} or runtime_condition_blocked
    source["next_run"] = None if condition_blocked else planned_at
    if background_mail and status == "waiting_idle" and runtime_status not in {"maintenance", "waiting_power"}:
        source["next_run"] = planned_at
        source["next_action"] = "邮件检查按计划继续；附件解析与业务分析等待空闲"
    source["last_attempt"] = newest_time(source.get("last_attempt"), row.get("last_attempt"))
    source["last_success"] = newest_time(source.get("last_success"), row.get("last_success"))
    source["last_failure"] = newest_time(source.get("last_failure"), row.get("last_failure"))
    source["automation_freshness"] = state_value(
        "fresh" if fresh else "stale",
        "自动证据新鲜" if fresh else "自动证据过期",
        "监督器心跳在有效期内" if fresh else "超过 150 秒没有监督器心跳",
    )
    current_alert = next(
        (
            alert for alert in alerts
            if str(alert.get("status") or "") == "open"
            and str(alert.get("effective_state") or "") in {"critical", "warning"}
        ),
        None,
    )
    if row.get("error_code") == "validation_inputs_changed" and not any(a.get("status") == "open" and a.get("error_code") != "validation_inputs_changed" and a.get("effective_state") == "critical" for a in alerts):
        view = source.get("validation_wait") or {}
        text = "聊天已同步，正在校验" if view.get("phase") == "validating" else "聊天已同步，等待资料稳定"
        if source["id"] != "dingtalk_chat":
            text = text.replace("聊天", "听记")
        if not view.get("sync_completed"):
            text = "校验前置步骤等待核实"
        detail = f"已等待 {int(view.get('wait_seconds') or 0) // 60} 分钟；下次检查：{view.get('next_retry_at') or row.get('next_due') or '后台自动安排'}"
        source["automatic_state"] = state_value("deferred", text, detail)
        source["next_action"] = "自动重验记忆；已完成的同步步骤保留"
        return
    if current_alert and str(current_alert.get("effective_state")) == "critical":
        action = str(current_alert.get("action_label") or "")
        detail = str(current_alert.get("detail") or "故障尚未获得同一来源的新成功证据")
        source["automatic_state"] = state_value(
            "persistent_failure", "需要人工安全确认" if action else "持续失败",
            f"{detail}{'；' + action if action and action not in detail else ''}",
        )
        return
    if current_alert:
        category = str(current_alert.get("category") or "")
        code = "manual_required" if category == "manual_attention" else (
            "waiting_source" if category == "waiting_source" else "retrying"
        )
        source["automatic_state"] = state_value(
            code,
            "需要人工处理" if code == "manual_required" else (
                "等待来源" if code == "waiting_source" else "状态仍待恢复"
            ),
            str(current_alert.get("detail") or "告警仍未关闭"),
        )
        return
    if maintenance_active and status not in {"failed", "persistent_failure"}:
        source["automatic_state"] = state_value("maintenance", "维护中", "后台监督器已安全暂停")
        return
    if not fresh:
        source["lifecycle"] = state_value("unknown", "后台状态过期", "超过 150 秒没有监督心跳")
        source["automatic_state"] = state_value("stale", "后台状态过期", "自动监督器心跳已过期")
        return
    source["lifecycle"] = state_value(
        "enabled", "统一监督已接管", "接电后正文和图片优先处理，其他附件分阶段补录" if source.get("phased_processing") else "接电后后台检查邮件；耗时处理等待空闲" if background_mail else "接电并满足空闲条件后按资格执行"
    )
    detail_by_status = {
        "retrying": f"已尝试 {int(row.get('attempt_count') or 0)} 次，将自动继续",
        "waiting_source": f"{int(row.get('attention_count') or 0)} 项等待上游来源；将持续探测",
        "manual_required": f"{int(row.get('attention_count') or 0)} 项需要人工查看，不会无意义重试",
        "waiting_idle": "邮件检查继续；附件解析与业务分析等待连续空闲 3 分钟" if background_mail else "等待电脑连续空闲 3 分钟",
        "waiting_power": "等待电脑重新接电",
        "persistent_failure": f"已连续失败 {int(row.get('attempt_count') or 0)} 次；每 6 小时继续探测",
        "deferred": "维护、锁或核心状态暂不允许写入；稍后自动检查",
    }
    status_values = {
        "running": state_value("syncing", "正在同步", "由后台监督器串行启动"),
        "retrying": state_value("retrying", "自动重试中", detail_by_status["retrying"]),
        "waiting_source": state_value("waiting_source", "等待来源", detail_by_status["waiting_source"]),
        "manual_required": state_value("manual_required", "需要人工处理", detail_by_status["manual_required"]),
        "waiting_idle": state_value("waiting_idle", "耗时处理等待空闲" if background_mail else "等待空闲", detail_by_status["waiting_idle"]),
        "waiting_power": state_value("waiting_power", "等待接电", detail_by_status["waiting_power"]),
        "persistent_failure": state_value("persistent_failure", "持续失败", detail_by_status["persistent_failure"]),
        "deferred": state_value("deferred", "安全延后", detail_by_status["deferred"]),
        "failed": state_value("failed", "自动任务失败", str(row.get("error_code") or "unknown")),
    }
    if status in status_values:
        source["automatic_state"] = status_values[status]
        if status == "running":
            source["activity"] = state_value("syncing", "正在同步", "后台监督器运行中")
        return
    recovered_at = parse_time(row.get("recovery_at"))
    last_failure = parse_time(row.get("last_failure"))
    if recovered_at and (not last_failure or recovered_at >= last_failure):
        source["automatic_state"] = state_value("recovered", "已恢复", "最近一次自动重试已经成功")
    elif status == "healthy":
        source["automatic_state"] = state_value("healthy", "正常空闲", "后台检查运行正常")
    else:
        source["automatic_state"] = state_value("unknown", "自动状态未知", status)


def finalize_source_state(
    source: dict[str, Any],
    now: datetime,
    maintenance_active: bool,
) -> None:
    """Merge manual, automatic and source evidence before deriving one effective status."""
    source.setdefault("manual_state", state_value("idle", "暂无手动任务"))
    managed = str(source.get("id") or "") in MANAGED_SOURCE_IDS
    if not managed:
        source.setdefault("automatic_state", json.loads(json.dumps(source.get("lifecycle") or {}, ensure_ascii=False)))
        source["automation_freshness"] = state_value("not_managed", "不由统一监督器管理")
    fresh_until = parse_time(source.get("fresh_until"))
    evidence_stale = bool(fresh_until and now > fresh_until)
    source["evidence_freshness"] = state_value(
        "stale" if evidence_stale else ("fresh" if fresh_until else "unknown"),
        "来源证据过期" if evidence_stale else ("来源证据新鲜" if fresh_until else "来源新鲜度未知"),
        f"有效至 {source.get('fresh_until')}" if source.get("fresh_until") else "没有可用的新鲜度时间",
    )

    last_success = source.get("last_success")
    last_failure = source.get("last_failure")
    attention_breakdown = (
        source.get("attention_breakdown")
        if isinstance(source.get("attention_breakdown"), dict)
        else {}
    )
    unresolved_technical = int(attention_breakdown.get("technical_failed") or 0) > 0
    newer_success = bool(
        not unresolved_technical
        and last_failure
        and later_than(last_success, last_failure)
    )
    if newer_success and str((source.get("health") or {}).get("code") or "") == "failed":
        source["health"] = state_value("recovered", "已恢复", "历史失败保留，更新的成功证据已覆盖当前故障")
        source["recovered"] = True
        if str((source.get("acceptance") or {}).get("code") or "") == "blocked":
            source["acceptance"] = state_value("accepted", "后续运行已验收")

    automatic = source.get("automatic_state") or state_value("unknown", "自动状态未知")
    automatic_code = str(automatic.get("code") or "unknown")
    health_code = str((source.get("health") or {}).get("code") or "unknown")
    acceptance_code = str((source.get("acceptance") or {}).get("code") or "unknown")
    lifecycle_code = str((source.get("lifecycle") or {}).get("code") or "unknown")

    if automatic_code in {"failed", "persistent_failure"}:
        effective = automatic
    elif health_code in {"failed", "blocked"} or acceptance_code == "blocked":
        effective = state_value(
            "failed", "需要处理",
            (source.get("health") or {}).get("detail") or (source.get("acceptance") or {}).get("detail", ""),
        )
    elif maintenance_active:
        effective = state_value("maintenance", "维护中", "当前只查看状态")
    elif str((source.get("activity") or {}).get("code") or "") in {"syncing", "writing"}:
        effective = state_value("syncing", "正在同步", (source.get("activity") or {}).get("detail", ""))
    elif evidence_stale and automatic_code in {
        "waiting_idle", "waiting_power", "deferred", "waiting_source", "manual_required"
    }:
        effective = json.loads(json.dumps(automatic, ensure_ascii=False))
        effective["detail"] = f"{effective.get('detail') or '等待合法执行条件'}；来源证据也已过期"
    elif evidence_stale:
        effective = state_value("failed", "来源状态证据过期", "当前没有可验证的合法阻挡条件")
    elif automatic_code == "stale":
        effective = state_value("stale", "自动监督证据过期", "统一监督器状态不能据此显示为绿色")
    elif automatic_code in {
        "retrying", "waiting_source", "manual_required", "waiting_idle", "waiting_power", "deferred"
    }:
        effective = automatic
    elif acceptance_code == "not_enrolled":
        effective = state_value("paused", "尚未完成登记", (source.get("acceptance") or {}).get("detail", ""))
    elif health_code in {"partial", "attention"} or acceptance_code in {"partial", "attention"}:
        effective = state_value(
            "partial", "部分完成",
            (source.get("acceptance") or {}).get("detail") or (source.get("health") or {}).get("detail", ""),
        )
    elif lifecycle_code in {"paused", "disabled"}:
        effective = state_value("paused", (source.get("lifecycle") or {}).get("label", "已暂停"), (source.get("lifecycle") or {}).get("detail", ""))
    elif health_code == "unknown" or lifecycle_code == "unknown" or automatic_code == "unknown":
        effective = state_value("unknown", "状态未知", (source.get("health") or {}).get("detail", ""))
    elif health_code == "recovered" or automatic_code == "recovered" or newer_success:
        effective = state_value("recovered", "已恢复", "历史失败保留，更新的成功证据已生效")
    else:
        effective = state_value("healthy", "正常空闲", "手动、自动与来源证据一致")
    source["display_status"] = effective
    source["effective_status"] = json.loads(json.dumps(effective, ensure_ascii=False))
    source["recovered"] = effective["code"] == "recovered"


def build_source_cards(
    paths: DashboardPaths,
    config: dict[str, Any],
    pipelines: list[dict[str, Any]],
    snapshot: Optional[dict[str, Any]],
    locks: list[dict[str, Any]],
    maintenance_active: bool,
    now: datetime,
    supervisor: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    by_id = {pipeline["id"]: pipeline for pipeline in pipelines}
    dingtalk_pipeline = by_id["dingtalk"]

    chat = clone_source(dingtalk_pipeline, "dingtalk_chat")
    chat["description"] = "聊天增量、覆盖核对与业务记忆"
    diagnostic, _ = read_json(paths.external_root / "dingtalk-chat/state/listener-diagnostic.json")
    supplement, _ = read_json(paths.external_root / "dingtalk-chat/state/listener-diagnostic-supplement-01.json")
    diagnostic_count = int(bool(diagnostic)) + int(bool(supplement))
    original_diagnostic = diagnostic
    diagnostic = supplement or diagnostic
    acceptance_receipt, _ = read_json(paths.external_root / "dingtalk-chat/state/listener-manual-acceptance.json")
    listener_state, _ = read_json(paths.dingtalk_automation_status)
    listener_accepted = ((listener_state or {}).get("live_acceptance") == "passed"
                         and (listener_state or {}).get("subscriptions_valid") is True
                         and (listener_state or {}).get("direct_ready") is True
                         and (listener_state or {}).get("group_ready") is True)
    chat["listener_diagnostic"] = {
        "state": (diagnostic or {}).get("state", "not_started"),
        "diagnostic_sessions_used": diagnostic_count,
        "additional_diagnostic_sessions_used": int(bool(supplement)),
        "original_diagnostic_preserved": bool(original_diagnostic),
        "acceptance_sessions_used": int(bool(acceptance_receipt)),
        "diagnostic_session_limit": 1 + int(bool(supplement)), "acceptance_session_limit": 1,
        "finished_at": (diagnostic or {}).get("finished_at"),
        "error_code": (diagnostic or {}).get("error_code"),
        "scopes": {kind: {key: value for key, value in item.items() if key in {
            "ready", "error_codes", "process_exit_code", "retryable", "retry_after_seconds"}}
            for kind, item in ((diagnostic or {}).get("scopes") or {}).items() if isinstance(item, dict)},
    }
    chat["components"] = filtered_components(dingtalk_pipeline, {"listener", "chat", "memory"})
    chat["legacy_scheduler"], chat["legacy_next_run"] = source_legacy_scheduler(
        chat["components"], {"chat", "memory"}
    )
    chat_state, chat_error = read_json(paths.dingtalk_chat_incremental_status)
    full_chat, full_chat_error = read_json(paths.dingtalk_chat_status)
    chat_rows = [row for row in (chat_state, full_chat) if isinstance(row, dict)]
    chat_rows.sort(
        key=lambda row: parse_time(row.get("completed_at") or row.get("started_at"))
        or datetime.min.replace(tzinfo=LOCAL_TZ),
        reverse=True,
    )
    latest_chat = chat_rows[0] if chat_rows else None
    incomplete = int((latest_chat or {}).get("conversations_incomplete") or 0)
    chat["last_attempt"] = (latest_chat or {}).get("started_at")
    chat["last_success"] = (
        (latest_chat or {}).get("completed_at")
        if (latest_chat or {}).get("status") == "success" and incomplete == 0 else None
    )
    chat["last_failure"] = (
        (latest_chat or {}).get("completed_at")
        if (latest_chat or {}).get("status") == "failed" else None
    )
    service_failed = any(
        (component.get("health") or {}).get("code") == "failed"
        for component in chat["components"]
    )
    if chat_error and full_chat_error:
        chat["health"] = state_value("unknown", "聊天状态未知")
        chat["acceptance"] = state_value("unknown", "覆盖状态未知")
    elif service_failed or (latest_chat or {}).get("status") == "failed":
        chat["health"] = state_value("failed", "最近同步异常", "聊天增量或调度器未正常结束")
        chat["acceptance"] = state_value("blocked", "尚未验收")
    elif (latest_chat or {}).get("status") == "partial" or incomplete:
        partial_detail = f"{incomplete} 个会话待补齐" if incomplete else "最近同步部分完成"
        chat["health"] = state_value("partial", "覆盖不完整", partial_detail)
        chat["acceptance"] = state_value("partial", "部分完成", partial_detail)
    else:
        chat["health"] = state_value("healthy", "运行正常")
        chat["acceptance"] = state_value("accepted", "聊天覆盖已完成")
    chat["attention"] = incomplete + int(service_failed) + int(
        (latest_chat or {}).get("status") == "partial" and incomplete == 0
    )
    chat["next_run"] = earliest_next(chat["components"])
    chat["fresh_until"] = future_iso(
        (latest_chat or {}).get("completed_at") or (latest_chat or {}).get("started_at"),
        2 * 60 * 60,
    )
    chat["evidence"] = [
        evidence(
            "钉钉聊天脱敏状态",
            "unknown" if chat_error and full_chat_error else "ok",
            safe_file_time(paths.dingtalk_chat_incremental_status),
            "仅含聚合计数与时间",
        )
    ]

    minutes = clone_source(dingtalk_pipeline, "dingtalk_minutes")
    minutes["description"] = "文字、原始媒体、来源整理与语义记忆"
    minutes["components"] = filtered_components(
        dingtalk_pipeline, {"minutes", "minutes_source", "minutes_semantic"}
    )
    minutes["legacy_scheduler"], minutes["legacy_next_run"] = source_legacy_scheduler(
        minutes["components"], {"minutes", "minutes_source", "minutes_semantic"}
    )
    minutes_state, minutes_error = read_json(paths.minutes_sync_state)
    inventory, inventory_error = read_json(paths.minutes_inventory)
    entries = (minutes_state or {}).get("entries") if isinstance((minutes_state or {}).get("entries"), dict) else {}
    media_missing = sum(
        1 for row in entries.values()
        if isinstance(row, dict) and "media" in {str(value) for value in (row.get("missing_artifacts") or [])}
    )
    remote_count = int((inventory or {}).get("last_complete_count") or 0)
    source_missing = max(0, remote_count - len(entries))
    minutes_attention = media_missing + source_missing
    attempt_status = str((inventory or {}).get("last_attempt_status") or "")
    minutes["last_attempt"] = (inventory or {}).get("last_attempt_at")
    minutes["last_success"] = newest_time(
        (minutes_state or {}).get("last_sync_at"),
        (inventory or {}).get("last_complete_at"),
    )
    minutes["last_failure"] = (
        (inventory or {}).get("last_attempt_at")
        if attempt_status not in {"", "complete", "success", "noop"} else None
    )
    minutes["next_run"] = earliest_next(minutes["components"])
    minutes["fresh_until"] = future_iso(minutes["last_success"], 2 * 60 * 60)
    if minutes_error or inventory_error:
        minutes["health"] = state_value("unknown", "听记状态未知")
        minutes["acceptance"] = state_value("unknown", "媒体验收未知")
    elif minutes["last_failure"]:
        minutes["health"] = state_value("failed", "最近同步失败", str((inventory or {}).get("last_error_code") or "unknown"))
        minutes["acceptance"] = state_value("blocked", "尚未验收")
    elif minutes_attention:
        minutes["health"] = state_value("partial", "部分完成", "文字已保留，媒体或来源仍待补齐")
        minutes["acceptance"] = state_value(
            "partial",
            "部分完成",
            f"媒体待补 {media_missing} 条；来源待同步 {source_missing} 条",
        )
    else:
        minutes["health"] = state_value("healthy", "运行正常")
        minutes["acceptance"] = state_value("accepted", "文字与媒体均完整")
    minutes["attention"] = minutes_attention
    minutes["evidence"] = [
        evidence(
            "听记同步状态",
            "unknown" if minutes_error else "ok",
            safe_file_time(paths.minutes_sync_state),
            f"已登记 {len(entries)} 条；媒体待补 {media_missing} 条",
        ),
        evidence(
            "听记远端清单",
            "unknown" if inventory_error else "ok",
            safe_file_time(paths.minutes_inventory),
            f"最近完整清单 {remote_count} 条",
        ),
    ]

    mail = clone_source(by_id["email"], "dingtalk_mail")
    mail["description"] = "企业邮箱归档、摘要与业务记忆"
    win11 = clone_source(by_id["win11"], "win11")
    win11["description"] = "Win11 只读来源监测与语义合并"
    merge_health, _ = read_json(paths.win11_health)
    blocked_items, _ = read_json(paths.external_root / "machine-ledgers/win11-blocked-items.json")
    win11["remaining_batches"] = (merge_health or {}).get("remaining_batches")
    win11["remaining_content_items"] = (merge_health or {}).get("remaining_content_items")
    win11["blocked_items"] = len((blocked_items or {}).get("items") or [])
    wechat = clone_source(by_id["wechat"], "wechat")
    wechat["description"] = "登记完成后启用的微信业务记忆"

    sources = [chat, minutes, mail, win11, wechat]
    active_action_row = next(
        (
            row for row in ((snapshot or {}).get("active_actions") or [])
            if isinstance(row, dict) and str(row.get("status") or "") == "running"
        ),
        None,
    )
    active_control_row = next(
        (
            row for row in ((snapshot or {}).get("active_actions") or [])
            if isinstance(row, dict)
            and str(row.get("action_type") or "") in set().union(*SOURCE_ACTION_TYPES.values())
        ),
        None,
    )
    active_control = active_control_row is not None
    controller_lock = next((item for item in locks if item.get("id") == "sync_control"), {})
    core = by_id["core"]
    core_display = str((core.get("display_status") or {}).get("code") or "unknown")
    core_acceptance = str((core.get("acceptance") or {}).get("code") or "unknown")
    global_reason_code = ""
    global_reason = ""
    if maintenance_active:
        global_reason_code, global_reason = "maintenance_active", "记忆库维护中"
    elif active_action_row is not None or active_control or controller_lock.get("held") is True:
        global_reason_code, global_reason = "already_running", "已有同步正在运行"
    elif core_display not in {"failed", "stale", "unknown", "maintenance"} and core_acceptance == "pending":
        global_reason_code, global_reason = "memory_core_unverified", "资料正在更新，等待与当前版本匹配的完整校验"
    elif core_display in {"failed", "stale", "unknown", "maintenance"} or core_acceptance != "accepted":
        global_reason_code, global_reason = "memory_core_unavailable", "记忆库完整性尚未通过，系统正在核对具体原因"
    elif (core.get("activity") or {}).get("code") == "writing":
        global_reason_code, global_reason = "memory_core_busy", "记忆库正在写入，完成后自动恢复操作"

    for source in sources:
        source["phased_processing"] = source["id"] == "dingtalk_mail" and config.get("email_sync", {}).get("phased_processing") is True
        source["background_collection_while_active"] = source["id"] == "dingtalk_mail" and config.get("email_sync", {}).get("background_collection_while_active") is True
        source["scheduler_owner"] = (
            "unified_supervisor" if source["id"] in MANAGED_SOURCE_IDS else "legacy"
        )
        running_id, latest_id = apply_control_action_state(source, snapshot, now, maintenance_active)
        if source["id"] != "wechat":
            row = ((supervisor or {}).get("sources") or {}).get(source["id"], {})
            if row.get("error_code") == "validation_inputs_changed":
                import validation_retry
                try:
                    source["validation_wait"] = validation_retry.wait_view(paths.state_root.parent, row, now, database=paths.supervisor_database)
                except (OSError, ValueError, RuntimeError):
                    source["validation_wait"] = {"phase": "waiting_stable", "next_retry_at": row.get("next_due")}
            apply_supervisor_source_state(source, supervisor or {}, now, maintenance_active)
        else:
            source["automatic_state"] = state_value(
                "paused" if source["lifecycle"]["code"] == "paused" else "healthy",
                "旧独立自动化已暂停" if source["lifecycle"]["code"] == "paused" else "旧独立自动化已启用",
            )
            if source["lifecycle"]["code"] == "paused":
                source["next_run"] = None
                source["next_action"] = "完成安全登记后启用"
        finalize_source_state(source, now, maintenance_active)
        if source["id"] == "dingtalk_mail" and source.get("processing_summary"):
            breakdown = source.get("attention_breakdown") or {}
            if (source["display_status"].get("code") == "manual_required"
                    and breakdown.get("technical_failed") == 0 and breakdown.get("pending") == 0):
                source["display_status"]["label"] = "邮件已处理，附件待核验"
                source["display_status"]["detail"] = source["processing_summary"]
            else:
                source["display_status"]["detail"] = source["processing_summary"] + "；" + source["display_status"].get("detail", "")
        if source["id"] == "dingtalk_chat":
            source["backfill_acceptance"] = dict(source["acceptance"])
            source["listener_acceptance"] = state_value("accepted" if listener_accepted else "pending",
                "实时新消息已验收" if listener_accepted else "实时监听未验收")
            if source["acceptance"]["code"] == "accepted" and not listener_accepted:
                source["acceptance"] = state_value("partial", "补同步已完成，监听未验收")
        reason_code = global_reason_code
        reason = global_reason
        if not reason_code and source["id"] == "dingtalk_mail" and not (
            (config.get("email_sync") or {}).get("enabled") is True
            and (config.get("email_sync") or {}).get("writer_registered") is True
        ):
            reason_code, reason = "email_not_configured", "邮件本地处理器尚未配置完成"
        if not reason_code and source["id"] == "wechat" and source["acceptance"]["code"] != "accepted":
            reason_code, reason = "enrollment_required", "微信尚未完成安全登记"
        required_path = {
            "dingtalk_chat": paths.dingtalk_chat_incremental_status.parent.parent.parent / "dingtalk-chat",
            "dingtalk_minutes": paths.minutes_sync_state.parent.parent / "sync_minutes.py",
            "dingtalk_mail": paths.root / "bin" / "email_sync.py",
            "win11": paths.root / "bin" / "business_sync_now.sh",
            "wechat": paths.root / "bin" / "run_wechat_pipeline.sh",
        }[source["id"]]
        if not reason_code and not required_path.exists():
            reason_code, reason = "source_unavailable", "同步入口不可用"
        source["control"] = {
            "available": not bool(reason_code),
            "reason": reason,
            "reason_code": reason_code or None,
            "target": source["id"],
            "local_only": True,
            "running": bool(running_id),
            "run_id": running_id or latest_id,
            "retry_available": not bool(reason_code) and source["display_status"]["code"] in {
                "failed", "retrying", "persistent_failure"
            },
            "view_issue": source["display_status"]["code"] == "manual_required",
        }

    memory_core = {
        "display_status": core.get("display_status"),
        "lifecycle": core.get("lifecycle"),
        "activity": core.get("activity"),
        "health": core.get("health"),
        "acceptance": core.get("acceptance"),
        "components": core.get("components"),
        "last_success": core.get("last_success"),
        "validation_current": core.get("validation_current"),
        "validation_error_code": core.get("validation_error_code"),
        "automatic_validation": core.get("automatic_validation"),
        "fresh_until": core.get("fresh_until"),
        "locks": [
            item for item in locks
            if item.get("id") in {"sync_control", "core_index", "receiver"}
        ],
    }
    inventory, _ = read_json(paths.external_root / "machine-ledgers/内部来源资产清单.json")
    registration, _ = read_json(paths.external_root / "machine-ledgers/source-inventory-status.json")
    memory_core["source_registration"] = {
        "registered_at": (inventory or {}).get("generated_at"),
        "record_count": (inventory or {}).get("record_count"),
        "missing_count": (registration or {}).get("missing_count", 0),
        "status": (registration or {}).get("status", "unknown"),
    }
    core_alert = next(
        (
            row for row in ((supervisor or {}).get("alerts") or [])
            if isinstance(row, dict)
            and str(row.get("source_id") or "") == "memory_core"
            and str(row.get("status") or "") == "open"
        ),
        None,
    )
    if core_alert:
        memory_core["alert"] = core_alert
        effective = str(core_alert.get("effective_state") or "unknown")
        if effective == "maintenance_pending":
            memory_core["display_status"] = state_value(
                "maintenance_pending", "维护后待复验",
                "维护窗口内的停机不判为当前损坏；维护退出后立即复检。",
            )
        elif effective == "critical" and not (
            core.get("validation_current") is True
            and (core.get("acceptance") or {}).get("code") == "accepted"
            and (core.get("health") or {}).get("code") in {"healthy", "recovered"}
            and later_than(core.get("last_success"), core_alert.get("last_seen"))
        ):
            memory_core["display_status"] = state_value(
                "failed", "记忆库核心需要处理",
                str(core_alert.get("detail") or "尚未获得核心来源的新成功证据"),
            )
            if not global_reason_code:
                global_reason_code, global_reason = (
                    "memory_core_unavailable", "记忆库核心尚未获得新的成功复检证据"
                )
        core["display_status"] = memory_core["display_status"]
    global_action = {
        "available": not bool(global_reason_code),
        "reason": global_reason,
        "reason_code": global_reason_code or None,
        "target": "all",
        "local_only": True,
        "running": bool(active_control_row),
        "run_id": str(active_control_row.get("run_id")) if active_control_row else None,
        "confirmation": "将依次同步钉钉聊天与听记、钉钉邮件、Win11；微信未登记时安全跳过。不会发送群通知或批次汇报。",
    }
    return sources, memory_core, global_action


def primary_overall(
    sources: list[dict[str, Any]],
    memory_core: dict[str, Any],
    maintenance_active: bool,
    automation: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    automation_status = str(((automation or {}).get("display_status") or {}).get("code") or "")
    if automation_status == "failed":
        return state_value(
            "failed", "统一监督器需要处理",
            str(((automation or {}).get("display_status") or {}).get("detail") or "后台监督器未正常运行"),
        )
    core_status = str((memory_core.get("display_status") or {}).get("code") or "unknown")
    if core_status == "failed":
        return state_value("failed", "记忆库核心需要处理", "写入、索引或校验尚未恢复")
    persistent = [source for source in sources if source["display_status"]["code"] == "persistent_failure"]
    failed = [source for source in sources if source["display_status"]["code"] == "failed"]
    technical_failures = persistent + [source for source in failed if source not in persistent]
    if technical_failures:
        return state_value(
            "failed",
            "有来源持续失败" if persistent else "有来源需要处理",
            "、".join(source["name"] for source in technical_failures),
        )
    retrying = [source for source in sources if source["display_status"]["code"] == "retrying"]
    if retrying:
        return state_value("retrying", "正在自动重试", "、".join(source["name"] for source in retrying))
    if automation_status in {"stale", "unknown"}:
        return state_value("stale", "统一监督状态未知或过期", "来源状态不能据此显示为全绿")
    if automation_status == "power_unknown":
        return state_value(
            "power_unknown", "电源状态待确认",
            "系统每分钟重新检测；未知状态不会冒充电池状态。",
        )
    if core_status == "maintenance_pending":
        return state_value(
            "maintenance_pending", "维护后待复验",
            "核心历史故障仍保留；维护退出后立即复检，不误报为当前损坏。",
        )
    if maintenance_active:
        return state_value("maintenance", "记忆库维护中", "当前只查看状态；既有技术失败仍会优先显示")
    syncing = [source for source in sources if source["display_status"]["code"] == "syncing"]
    if syncing or core_status == "syncing":
        names = [source["name"] for source in syncing]
        return state_value("syncing", "正在同步", "、".join(names) or "记忆库正在写入")
    manual = [source for source in sources if source["display_status"]["code"] == "manual_required"]
    if manual:
        return state_value("manual_required", "有项目需要人工处理", "、".join(source["name"] for source in manual))
    partial = [source for source in sources if source["display_status"]["code"] == "partial"]
    if partial:
        return state_value("partial", "有来源部分完成", "、".join(source["name"] for source in partial))
    missing = int((memory_core.get("source_registration") or {}).get("missing_count") or 0)
    if missing:
        return state_value("partial", "仍有登记资料缺失", f"{missing} 份资料保留缺失记录，完整同步尚未验收")
    waiting_source = [source for source in sources if source["display_status"]["code"] == "waiting_source"]
    if waiting_source:
        return state_value("waiting_source", "有来源仍在等待", "、".join(source["name"] for source in waiting_source))
    waiting_idle = [source for source in sources if source["display_status"]["code"] == "waiting_idle"]
    if waiting_idle:
        return state_value("waiting_idle", "等待电脑空闲", "连续空闲 3 分钟后自动继续")
    waiting_power = [source for source in sources if source["display_status"]["code"] == "waiting_power"]
    if waiting_power:
        return state_value("waiting_power", "等待电脑接电", "重新接电后立即补查")
    deferred = [source for source in sources if source["display_status"]["code"] == "deferred"]
    if deferred:
        return state_value("deferred", "等待安全条件", "、".join(source["name"] for source in deferred))
    if automation_status == "waiting_idle":
        return state_value("waiting_idle", "等待电脑空闲", "连续空闲 3 分钟后自动继续")
    if automation_status == "waiting_power":
        return state_value("waiting_power", "等待电脑接电", "重新接电后立即继续")
    unknown = [source for source in sources if source["display_status"]["code"] in {"stale", "unknown"}]
    if core_status in {"stale", "unknown"} or unknown:
        return state_value("stale", "有状态未知或过期", "、".join(source["name"] for source in unknown))
    paused = [source for source in sources if source["display_status"]["code"] == "paused"]
    if paused:
        return state_value("paused", "部分自动同步保持暂停", "、".join(source["name"] for source in paused))
    recovered = [source for source in sources if source["display_status"]["code"] == "recovered"]
    if recovered or core_status == "recovered":
        return state_value("recovered", "同步中心已恢复", "历史失败仍保留")
    return state_value("healthy", "全部来源正常空闲", "当前没有同步任务")


def automation_summary(
    supervisor: dict[str, Any],
    supervisor_error: Optional[str],
    service: dict[str, Any],
    now: datetime,
    maintenance_active: bool,
    sentinel_service: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    runtime = supervisor.get("runtime") if isinstance(supervisor.get("runtime"), dict) else {}
    source_rows = supervisor.get("sources") if isinstance(supervisor.get("sources"), dict) else {}
    alerts = [row for row in (supervisor.get("alerts") or []) if isinstance(row, dict)]
    heartbeat = parse_time(runtime.get("heartbeat_at"))
    heartbeat_fresh = bool(heartbeat and now - heartbeat <= timedelta(seconds=150))
    runtime_status = str(runtime.get("status") or "unknown")
    last_exit_code = service.get("last_exit_code")
    if runtime_status == "failed":
        status = state_value("failed", "后台监督运行失败", str(runtime.get("blocked_reason") or "supervisor_runtime_failed"))
    elif isinstance(last_exit_code, int) and last_exit_code != 0:
        status = state_value("failed", "后台监督异常退出", f"退出码 {last_exit_code}")
    elif maintenance_active:
        status = state_value("maintenance", "维护中", "后台任务已安全暂停")
    elif service.get("enabled") is False or not service.get("loaded"):
        status = state_value("failed", "后台监督未运行", "登录后台任务未启用或未加载")
    elif not heartbeat_fresh:
        status = state_value("failed", "后台监督心跳过期", supervisor_error or "超过 150 秒没有巡检心跳")
    elif runtime_status == "running":
        status = state_value("syncing", "后台正在同步", str(runtime.get("current_stage") or "串行处理中"))
    elif runtime_status == "waiting_power":
        status = state_value("waiting_power", "等待电脑接电", "接电后立即恢复来源检查")
    elif runtime_status == "power_unknown":
        status = state_value(
            "power_unknown", "电源状态未知",
            "未知不会按电池处理；系统每分钟使用备用方式重新检测。",
        )
    elif runtime_status == "waiting_idle":
        status = state_value("waiting_idle", "等待电脑空闲", "连续空闲 3 分钟后继续")
    elif runtime_status == "idle":
        status = state_value("healthy", "后台监督正常", "每分钟巡检；每个来源最多每 5 分钟发现一次")
    else:
        status = state_value("unknown", "后台监督状态未知", runtime_status)
    automation = {
        "display_status": status,
        "runtime_status": runtime_status,
        "enabled": service.get("enabled"),
        "loaded": service.get("loaded"),
        "launchd_state": service.get("state"),
        "last_exit_code": last_exit_code,
        "blocked_reason": runtime.get("blocked_reason"),
        "next_eligible_at": runtime.get("next_eligible_at"),
        "heartbeat_fresh": heartbeat_fresh,
        "last_check": runtime.get("heartbeat_at"),
        "next_check": future_iso(runtime.get("heartbeat_at"), 60),
        "last_scan": runtime.get("last_scan_at"),
        "next_scan": runtime.get("next_scan_at"),
        "current_source": runtime.get("current_source"),
        "current_stage": runtime.get("current_stage"),
        "pending_count": int(runtime.get("pending_count") or 0),
        "check_interval_seconds": 60,
        "discovery_interval_seconds": 300,
        "health_monitor": {
            "enabled": (sentinel_service or {}).get("enabled"),
            "loaded": (sentinel_service or {}).get("loaded"),
            "last_exit_code": (sentinel_service or {}).get("last_exit_code"),
            "schedule": (sentinel_service or {}).get("schedule"),
        },
    }
    power = {
        "source": str(runtime.get("power_source") or "unknown"),
        "on_ac": bool(runtime.get("on_ac")),
        "known": str(runtime.get("power_source") or "unknown") in {"ac", "battery"},
        "error_code": runtime.get("power_error_code"),
        "probe_failures": int(runtime.get("power_probe_failures") or 0),
        "observed_at": runtime.get("heartbeat_at"),
        "last_changed_at": runtime.get("last_power_change_at"),
    }
    idle = {
        "seconds": runtime.get("idle_seconds"),
        "ready": bool(runtime.get("idle_ready")),
        "required_seconds": 180,
        "observed_at": runtime.get("heartbeat_at"),
    }
    retry = {
        source_id: {
            "state": row.get("status"), "attempts": int(row.get("attempt_count") or row.get("probe_count") or 0),
            "next_retry": row.get("next_due"), "error_code": row.get("error_code"),
        }
        for source_id, row in source_rows.items() if isinstance(row, dict)
    }
    open_alerts = [
        row for row in alerts
        if str(row.get("status")) == "open"
        and str(row.get("effective_state") or "") in {"critical", "warning"}
    ]
    maintenance_pending = [
        row for row in alerts
        if str(row.get("status")) == "open"
        and str(row.get("effective_state") or "") == "maintenance_pending"
    ]
    alert = {
        "open_count": len(open_alerts),
        "maintenance_pending_count": len(maintenance_pending),
        "reporting_failed_count": sum(
            1 for row in alerts if (row.get("delivery") or {}).get("summary") == "failed"
        ),
        "unacknowledged_count": sum(1 for row in open_alerts if not row.get("acknowledged_at")),
        "latest": {
            key: alerts[0].get(key)
            for key in ("alert_id", "source_id", "category", "severity", "title", "last_seen", "status")
        } if alerts else None,
    }
    return automation, power, idle, retry, alert


class SystemDashboard:
    """Small in-memory cache around bounded read-only adapters."""

    def __init__(
        self,
        paths: DashboardPaths,
        runner: Callable[[list[str], float], CommandResult] = default_runner,
        cache_seconds: float = 4.0,
    ):
        self.paths = paths
        self.runner = runner
        self.cache_seconds = cache_seconds
        self._cache_lock = threading.Lock()
        self._collect_lock = threading.Lock()
        self._cache_generation = 0
        self._cached_at = 0.0
        self._cached_state: Optional[dict[str, Any]] = None
        self._cached_events: list[dict[str, Any]] = []
        self._binding_lock = threading.Lock()
        self._file_bindings: dict = {}

    def warm_file_bindings(self) -> None:
        """Read bytes before admitting UI requests; never cache a success state."""
        with self._binding_lock:
            try:
                validation_evidence.context(self.paths.root, file_cache=self._file_bindings)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                # The normal adapter will expose the precise current failure.
                # Keep individually verified digests; each future hit checks
                # current permissions and identity, even after a changed input.
                pass

    def _core(self, launchd, locks, current):
        with self._binding_lock:
            return adapt_core(self.paths, launchd, locks, current, file_cache=self._file_bindings)

    def collect(
        self,
        config: dict[str, Any],
        dingtalk_snapshot: Optional[dict[str, Any]] = None,
        force: bool = False,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        with self._cache_lock:
            generation = self._cache_generation
            if (not force and self._cached_state is not None
                    and time.monotonic() - self._cached_at < self.cache_seconds):
                return self._cached_state
        # A cache miss admits one collector. Concurrent requests can use the
        # freshly completed collection rather than repeat all file/lock probes.
        # Force still requires a collection completed after this request began.
        with self._collect_lock:
            with self._cache_lock:
                if (self._cached_state is not None
                        and time.monotonic() - self._cached_at < self.cache_seconds
                        and (not force or self._cache_generation != generation)):
                    return self._cached_state
            return self._collect_uncached(config, dingtalk_snapshot, force=True, now=now)

    def _collect_uncached(
        self,
        config: dict[str, Any],
        dingtalk_snapshot: Optional[dict[str, Any]] = None,
        force: bool = False,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        current = now or datetime.now().astimezone()
        with self._cache_lock:
            if (
                not force
                and self._cached_state is not None
                and time.monotonic() - self._cached_at < self.cache_seconds
            ):
                return self._cached_state
        labels = {
            "com.example.business-sync-supervisor",
            "com.example.business-sync-supervisor-health",
            "com.example.business-dingtalk-memory-listener",
            "com.example.business-dingtalk-chat-sync",
            "com.example.business-dingtalk-memory-hourly",
            "com.example.dingtalk-minutes-sync",
            "com.example.dingtalk-minutes-memory-source",
            "com.example.dingtalk-minutes-memory-semantic",
            "com.example.dingtalk-minutes-review",
            "com.example.business-enterprise-email-sync",
            "com.example.business-win11-memory-hourly",
            "com.example.business-wechat-chat-sync",
            "com.example.business-memory-bridge",
            "com.example.business-memory-folder-watcher",
        }
        launchd = LaunchdReader(self.paths, self.runner, current).read_many(labels)
        locks = lock_inventory(self.paths, self.runner)
        maintenance, maintenance_error = read_json(self.paths.maintenance_state)
        maintenance_active = bool(maintenance_error or (maintenance or {}).get("status") != "inactive")
        supervisor, supervisor_error = read_supervisor_snapshot(
            self.paths.supervisor_database,
            maintenance_active=maintenance_active,
            now=current,
        )

        pipelines: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        queues: dict[str, Optional[int]] = {"inbox": None, "watch_inbox": None}
        adapters = (
            ("dingtalk", lambda: adapt_dingtalk(self.paths, launchd, locks, dingtalk_snapshot, current)),
            ("email", lambda: adapt_email(self.paths, config, launchd, locks, current)),
            ("win11", lambda: adapt_win11(self.paths, launchd, locks, current)),
            ("wechat", lambda: adapt_wechat(self.paths, launchd, locks, current)),
            ("chatgpt", lambda: adapt_chatgpt(self.paths, current)),
            ("core", lambda: self._core(launchd, locks, current)),
        )
        for pipeline_id, adapter in adapters:
            try:
                result = adapter()
                if pipeline_id == "core":
                    pipeline, pipeline_events, queues = result
                else:
                    pipeline, pipeline_events = result
                pipelines.append(pipeline)
                events.extend(pipeline_events)
            except Exception:
                pipelines.append(unknown_pipeline(pipeline_id, current))
                events.append(make_event(
                    pipeline_id, "adapter", "failed", f"{PIPELINE_NAMES[pipeline_id]}状态读取失败",
                    "单个来源已降级为未知，其他流程继续显示", now_iso(current),
                ))
        by_id = {pipeline["id"]: pipeline for pipeline in pipelines}
        pipelines = [by_id[pipeline_id] for pipeline_id in PIPELINE_ORDER]
        for pipeline in pipelines:
            pipeline["display_status"] = display_status(pipeline, current, maintenance_active)
        for alert_row in supervisor.get("alerts") or []:
            if not isinstance(alert_row, dict):
                continue
            pipeline_id = SOURCE_PIPELINES.get(str(alert_row.get("source_id") or ""))
            if not pipeline_id:
                continue
            effective_alert = str(alert_row.get("effective_state") or "unknown")
            alert_status = {
                "critical": "failed",
                "warning": "attention",
                "maintenance_pending": "maintenance",
                "recovered": "recovered",
                "resolved": "success",
            }.get(effective_alert, "unknown")
            events.append(make_event(
                pipeline_id, "supervisor_alert", alert_status,
                str(alert_row.get("title") or "后台状态提醒"),
                str(alert_row.get("detail") or "仅含脱敏状态"),
                alert_row.get("last_seen") or now_iso(current),
            ))
        sources, memory_core, global_action = build_source_cards(
            self.paths,
            config,
            pipelines,
            dingtalk_snapshot,
            locks,
            maintenance_active,
            current,
            supervisor,
        )
        automation, power, idle, retry, alert = automation_summary(
            supervisor,
            supervisor_error,
            launchd.get("com.example.business-sync-supervisor", {}),
            current,
            maintenance_active,
            launchd.get("com.example.business-sync-supervisor-health", {}),
        )
        events.sort(key=lambda item: parse_time(item.get("time")) or datetime.min.replace(tzinfo=LOCAL_TZ), reverse=True)
        events = fold_events(events)
        active_syncs = [
            {
                "id": source["id"],
                "name": source["name"],
                "activity": source["activity"]["label"],
            }
            for source in sources
            if source["activity"]["code"] in {"syncing", "writing"}
        ]
        attention_by_source = {
            source["id"]: max(
                int(source.get("attention") or 0),
                int(source["display_status"]["code"] in ATTENTION_STATUS_CODES),
            )
            for source in sources
        }
        attention_count = sum(
            1 for source in sources
            if source["display_status"]["code"] in ABNORMAL_SOURCE_STATUS_CODES
        )
        core_attention = int(
            str((memory_core.get("display_status") or {}).get("code") or "")
            in {"failed", "stale", "unknown"}
        )
        result = {
            "schema_version": 4,
            "legacy_schema_version": 3,
            "generated_at": now_iso(current),
            "fresh_until": future_iso(now_iso(current), 90),
            "overall": primary_overall(sources, memory_core, maintenance_active, automation),
            "legacy_overall": overall_status(pipelines, maintenance_active, current),
            "active_syncs": active_syncs,
            "attention_count": attention_count,
            "attention_by_source": attention_by_source,
            "attention_count_note": "技术异常来源按来源计数；人工处理与等待对象只保留各来源自己的单位，不跨单位相加解释。",
            "core_attention": core_attention,
            "sources": sources,
            "memory_core": memory_core,
            "global_action": global_action,
            "automation": automation,
            "power": power,
            "idle": idle,
            "retry": retry,
            "alert": alert,
            "pipelines": pipelines,
            "locks": locks,
            "maintenance": {
                "status": "unknown" if maintenance_error else str((maintenance or {}).get("status") or "unknown"),
                "active": maintenance_active,
                "entered_at": (maintenance or {}).get("entered_at"),
                "exited_at": (maintenance or {}).get("exited_at"),
                "reason": "状态文件不可用" if maintenance_error else str((maintenance or {}).get("reason") or ""),
            },
            "queues": queues,
            "privacy": {
                "content_read": False,
                "wechat_raw_database_read": False,
                "keychain_read": False,
                "control_actions_added": True,
                "local_only": True,
            },
        }
        with self._cache_lock:
            self._cached_state = result
            self._cached_events = events
            self._cached_at = time.monotonic()
            self._cache_generation += 1
        return result

    def events(
        self,
        config: dict[str, Any],
        dingtalk_snapshot: Optional[dict[str, Any]] = None,
        pipeline: Optional[str] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if pipeline and pipeline not in PIPELINE_ORDER:
            raise ValueError("pipeline_invalid")
        state = self.collect(config, dingtalk_snapshot)
        with self._cache_lock:
            events = list(self._cached_events)
        if pipeline:
            events = [item for item in events if item["pipeline"] == pipeline]
        bounded = max(1, min(50, int(limit)))
        return {
            "generated_at": state["generated_at"],
            "pipeline": pipeline,
            "events": events[:bounded],
        }


def build_dashboard(
    config: dict[str, Any],
    dingtalk_snapshot: Optional[dict[str, Any]] = None,
    force: bool = False,
) -> dict[str, Any]:
    paths = DashboardPaths.from_config(config)
    return SystemDashboard(paths).collect(config, dingtalk_snapshot, force=force)
