#!/usr/bin/env python3
"""Automate DingTalk chat ingestion and governed BUSINESS business-memory updates.

The event listener and history reconciler are read-only toward DingTalk. Raw
identifiers and redacted source text stay in the local .nosync database. Codex
only emits a structured proposal in a read-only ephemeral run; a deterministic
writer validates and applies approved note shapes under the existing memory
index lock.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import chat_read_recovery
import sync_dingtalk_chat as chat_sync
import dingtalk_sync_v2 as syncv2
import business_page_router as business_router
import claim_reconciliation as reconciliation
import dingtalk_batch_contract as batch_contract
import dingtalk_commit_guard as commit_guard
from listener_retry_budget import ListenerBudget, BudgetBlocked, parse_time as budget_time


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
MIMO_API_KEY_FILE = ROOT / "state" / "mimo_api_key"

# --- Xiaomi MiMo Token Plan provider (2020-01-02 Example Owner decision) ---
# 2020-01-02: MiMo quota exhausted; paused in favour of DeepSeek V4.1 Flash.
# The MiMo constants and helpers stay for restoration.
MIMO_PROVIDER_ID = "mimo"
MIMO_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.5-pro"
MIMO_VISION_MODEL = "mimo-v2.5"  # omni-modal sibling for image/video understanding

# --- DeepSeek provider (2020-01-02 Example Owner decision) ---
# DeepSeek V4.1 Flash replaces MiMo as the default governed model family.
# Like MiMo it is called over OpenAI-compatible HTTP (see model_router), and
# the JSON contract still travels inside the prompt with caller-side schema
# validation.
DEEPSEEK_PROVIDER_ID = "deepseek"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"  # DeepSeek V4.1 Flash, natively multimodal
DEEPSEEK_API_KEY_FILE = ROOT / "state" / "deepseek_api_key"


def deepseek_api_key() -> str:
    try:
        key = DEEPSEEK_API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AutomationError("deepseek_key_unavailable", "DeepSeek key file is missing") from exc
    if not key.startswith("sk-") or len(key) < 20:
        raise AutomationError("deepseek_key_invalid", "DeepSeek key file is malformed")
    return key


def mimo_api_key() -> str:
    try:
        key = MIMO_API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AutomationError("mimo_key_unavailable", "MiMo Token Plan key file is missing") from exc
    if not key.startswith("tp-") or len(key) < 20:
        raise AutomationError("mimo_key_invalid", "MiMo Token Plan key file is malformed")
    return key


def mimo_provider_flags() -> list[str]:
    return [
        "-c", f'model_provider="{MIMO_PROVIDER_ID}"',
        "-c", f'model_providers.{MIMO_PROVIDER_ID}.name="{MIMO_PROVIDER_ID}"',
        "-c", f'model_providers.{MIMO_PROVIDER_ID}.base_url="{MIMO_BASE_URL}"',
        "-c", f'model_providers.{MIMO_PROVIDER_ID}.env_key="MIMO_API_KEY"',
        "-c", f'model_providers.{MIMO_PROVIDER_ID}.wire_api="responses"',
        "-c", 'web_search="disabled"',
    ]


def deepseek_provider_flags() -> list[str]:
    """Codex routing flags for the DeepSeek provider (Responses API, official)."""
    return [
        "-c", f'model_provider="{DEEPSEEK_PROVIDER_ID}"',
        "-c", f'model_providers.{DEEPSEEK_PROVIDER_ID}.name="{DEEPSEEK_PROVIDER_ID}"',
        "-c", f'model_providers.{DEEPSEEK_PROVIDER_ID}.base_url="{DEEPSEEK_BASE_URL}"',
        "-c", f'model_providers.{DEEPSEEK_PROVIDER_ID}.env_key="DEEPSEEK_API_KEY"',
        "-c", f'model_providers.{DEEPSEEK_PROVIDER_ID}.wire_api="responses"',
        "-c", 'web_search="disabled"',
    ]


def mimo_schema_prompt(schema_path: Path) -> str:
    """Embed the output JSON contract in the prompt (gateway has no json_schema)."""
    schema_text = Path(schema_path).read_text(encoding="utf-8")
    return (
        "\n\n<output_contract>\n"
        "你的最终回复必须只是一个 JSON 对象：不得包含任何解释、前后缀文字或 Markdown 代码块标记。\n"
        "该 JSON 对象必须严格符合以下 JSON Schema：\n"
        + schema_text
        + "\n</output_contract>\n"
    )


def parse_model_json(text: str) -> Any:
    """Parse the model's final message as JSON, tolerating fenced code blocks."""
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"\A```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*\Z", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AutomationError("codex_invalid_json", "Model did not return valid structured JSON") from exc


# 2020-01-02 Example Owner decision: MiMo Token Plan is the default model family,
# but the GPT-6 Astra subscription models and Claude Opus 5 stay available for
# explicit on-request switches. Default lives in config.json; switching is a
# config change plus this allowlist gate.
GOVERNED_ALLOWED_MODELS = {
    "deepseek-flash",                                                 # default (DeepSeek V4.1 Flash, 2020-01-02)
    "mimo-v2.5-pro", "mimo-v2.5",                                    # paused (MiMo quota exhausted; restorable)
    "kimi-for-coding", "k3",                                          # Kimi Code; K3 is the chain default (2020-01-02)
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",  # ChatGPT subscription login
    "claude-opus-5",                                                  # reserved; needs endpoint/key before use
}


def governed_provider_flags(model: str) -> list[str]:
    """Provider routing flags by model family. DeepSeek is the default."""
    model = str(model or DEEPSEEK_MODEL)
    if model.startswith("deepseek"):
        deepseek_api_key()  # fail fast when the default family lacks its key
        return deepseek_provider_flags()
    if model.startswith("mimo"):
        mimo_api_key()  # fail fast when the default family lacks its key
        return mimo_provider_flags()
    if model.startswith("claude"):
        return [
            "-c", 'model_provider="claude"',
            "-c", 'model_providers.claude.name="claude"',
            "-c", f'model_providers.claude.base_url="{os.environ.get("CLAUDE_BASE_URL", "https://api.anthropic.com/v1")}"',
            "-c", 'model_providers.claude.env_key="CLAUDE_API_KEY"',
            "-c", 'model_providers.claude.wire_api="responses"',
            "-c", 'web_search="disabled"',
        ]
    # GPT family: ChatGPT-subscription login, original routing (no override).
    return []


LISTENER_LOCK = ROOT / "state" / "dingtalk_memory_listener.lock"
HOURLY_LOCK = ROOT / "state" / "dingtalk_memory_hourly.lock"
MEMORY_LOCK = ROOT / "state" / "memory-index.lock"
SHARED_AI_LOCK = ROOT / "state" / "dingtalk-shared-ai.lock"
PROMPT_PATH = ROOT / "prompts" / "dingtalk_memory_proposal.md"
SCHEMA_PATH = ROOT / "schemas" / "dingtalk_memory_proposal.schema.json"
REFRESH_SCRIPT = ROOT / "bin" / "refresh_memory_indexes.rb"
VALIDATE_SCRIPT = ROOT / "bin" / "validate_memory.rb"

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b|\bAKIA[0-9A-Z]{16}\b|BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY")
UNSAFE_MARKDOWN_RE = re.compile(r"(?im)(^---[ \t]*$|^#{1,6}[ \t]|<!--|-->|<script|<iframe|!\[\[|\[\[|\]\])")
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)(.*)\Z", re.DOTALL)
MEMORY_ID_RE = re.compile(r"\ABUSINESS-(\d{8})-(\d{3})\Z")
UUID_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
CODEX_DISABLED_FEATURES = (
    "plugins",
    "apps",
    "remote_plugin",
    "plugin_sharing",
    "skill_search",
    "skill_mcp_dependency_install",
    "hooks",
    "memories",
    "multi_agent",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "image_generation",
    "shell_tool",
    "unified_exec",
)


class AutomationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        notification_detail: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.notification_detail = dict(notification_detail or {})


def failure_notification_detail(exc: BaseException, *, default_phase: str = "chat_memory") -> dict[str, Any]:
    if isinstance(exc, AutomationError):
        detail = dict(exc.notification_detail)
        detail.setdefault("phase", default_phase)
        detail.setdefault("reason", exc.message)
        return detail
    if isinstance(exc, subprocess.TimeoutExpired):
        return {
            "phase": default_phase,
            "reason": "本地子步骤超过执行时限，未能完成验收。",
            "auto_repair": "not_classified",
            "attempts": 1,
        }
    return {
        "phase": default_phase,
        "reason": "本地处理出现未分类异常，原队列已保留。",
        "auto_repair": "not_available",
        "attempts": 0,
    }


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    chat_sync.atomic_write(path, text, mode=mode)


def append_log(path: Path, event: str, **fields: Any) -> None:
    chat_sync.append_log(path, event, **fields)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_path(path: Path) -> Optional[str]:
    if not path.is_file() or path.is_symlink():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("dingtalk_chat"), dict):
        raise AutomationError("invalid_config", "dingtalk_chat config must be an object")
    if not isinstance(payload.get("dingtalk_automation"), dict):
        raise AutomationError("invalid_config", "dingtalk_automation config must be an object")
    automation = payload["dingtalk_automation"]
    fixed_values = {
        "enabled": True,
        "scope": "all_visible_direct_and_group_chats",
        "semantic_minutes": [5, 35],
        "reasoning_effort": "high",
        "subscription_only": True,
        "allow_paid_api_fallback": False,
        "allow_model_fallback": False,
        "semantic_session": "ephemeral",
        "semantic_sandbox": "read-only",
        "attachment_policy": ("unreadable_to_win11_v1" if payload.get("dingtalk_attachment_handoff", {}).get("enabled") is True
                              else "resource_refs_only_no_download"),
        "write_policy": "graded_trusted_writer",
    }
    for key, expected in fixed_values.items():
        if automation.get(key) != expected:
            raise AutomationError("invalid_config", f"governed automation setting is invalid: {key}")
    model = str(automation.get("model") or "")
    if model not in GOVERNED_ALLOWED_MODELS:
        raise AutomationError(
            "invalid_config",
            "governed model must be one of: " + ",".join(sorted(GOVERNED_ALLOWED_MODELS)),
        )
    if not 1 <= int(automation.get("max_batch_messages", 0)) <= 100:
        raise AutomationError("invalid_config", "max_batch_messages must be between 1 and 100")
    if not 1 <= int(automation.get("max_batch_chars", 0)) <= 30000:
        raise AutomationError("invalid_config", "max_batch_chars must be between 1 and 30000")
    bounded_settings = {
        "memory_index_refresh_timeout_seconds": (30, 300, 120),
        "memory_validation_timeout_seconds": (30, 300, 120),
        "local_verification_attempts": (1, 3, 3),
        "local_verification_retry_delay_seconds": (0, 30, 5),
    }
    for key, (minimum, maximum, default) in bounded_settings.items():
        value = int(automation.get(key, default))
        if not minimum <= value <= maximum:
            raise AutomationError("invalid_config", f"{key} must be between {minimum} and {maximum}")
    return payload


def runtime_paths(config: dict[str, Any]) -> dict[str, Path]:
    external_root = Path(str(config["external_root"]))
    memory_root = Path(str(config["memory_root"]))
    chat_root = external_root / "dingtalk-chat"
    automation_root = chat_root / "automation"
    return {
        "external_root": external_root,
        "memory_root": memory_root,
        "chat_root": chat_root,
        "database": chat_root / "state" / "chat.sqlite",
        "status": chat_root / "state" / "automation_status.json",
        "log": chat_root / "logs" / "automation.jsonl",
        "runs": automation_root / "runs",
        "alert_workdir": automation_root / "codex-alerts",
    }


def connect_database(config: dict[str, Any]) -> sqlite3.Connection:
    connection = chat_sync.connect_database(runtime_paths(config)["database"])
    try:
        # A conflict is a durable execution restriction, not a transient error label.
        connection.execute("CREATE TABLE IF NOT EXISTS semantic_recovery_holds (batch_id TEXT PRIMARY KEY, reason_code TEXT NOT NULL, created_at TEXT NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO semantic_recovery_holds SELECT batch_id,error_code,created_at FROM semantic_batches WHERE error_code IN ('proposal_commit_content_conflict','proposal_commit_id_conflict')")
        connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise


def acquire_lock(path: Path, blocking: bool = False) -> Any:
    ensure_private_dir(path.parent)
    handle = path.open("a+")
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError as exc:
        handle.close()
        raise AutomationError("already_running", f"lock is already held: {path.name}") from exc
    path.chmod(0o600)
    return handle


def status_update(config: dict[str, Any], **updates: Any) -> None:
    paths = runtime_paths(config)
    lock_path = paths["status"].with_suffix(".lock")
    lock = acquire_lock(lock_path, blocking=True)
    try:
        current: dict[str, Any] = {}
        if paths["status"].is_file():
            try:
                loaded = json.loads(paths["status"].read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = loaded
            except (OSError, json.JSONDecodeError):
                current = {}
        current.update(updates)
        current["schema_version"] = chat_sync.SCHEMA_VERSION
        current["updated_at"] = now_iso()
        atomic_write(paths["status"], json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    finally:
        lock.close()


def safe_status_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    paths = runtime_paths(config)
    status: dict[str, Any] = {}
    if paths["status"].is_file():
        try:
            loaded = json.loads(paths["status"].read_text(encoding="utf-8"))
            status = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            status = {"status_read_error": True}
    connection = connect_database(config)
    try:
        queue_counts = {
            str(row["state"]): int(row["count"])
            for row in connection.execute("SELECT state,COUNT(*) AS count FROM semantic_queue GROUP BY state")
        }
        event_counts = {
            str(row["state"]): int(row["count"])
            for row in connection.execute("SELECT state,COUNT(*) AS count FROM event_inbox GROUP BY state")
        }
        latest_batch = connection.execute(
            "SELECT batch_id,status,created_at,completed_at,message_count,error_code FROM semantic_batches ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        latest_incremental = connection.execute(
            "SELECT status,completed_at,window_start,window_end,messages_inserted,messages_updated,messages_hidden,error_code "
            "FROM sync_runs WHERE mode='incremental' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        conversation_coverage = connection.execute(
            """
            SELECT COUNT(*) AS known,
                   SUM(CASE WHEN last_complete_scan_at IS NOT NULL THEN 1 ELSE 0 END) AS complete,
                   SUM(CASE WHEN last_complete_scan_at IS NULL THEN 1 ELSE 0 END) AS pending
            FROM conversations
            """
        ).fetchone()
        status.update(
            {
                "database_quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
                "database_user_version": connection.execute("PRAGMA user_version").fetchone()[0],
                "database_journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "database_secure_delete": connection.execute("PRAGMA secure_delete").fetchone()[0],
                "database_file_mode": oct(paths["database"].stat().st_mode & 0o777),
                "unique_messages": connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "conversation_coverage": {
                    "known": int(conversation_coverage["known"] or 0),
                    "historically_complete": int(conversation_coverage["complete"] or 0),
                    "pending_history": int(conversation_coverage["pending"] or 0),
                },
                "queue": queue_counts,
                "event_inbox": event_counts,
                "latest_batch": dict(latest_batch) if latest_batch else None,
                "latest_incremental": dict(latest_incremental) if latest_incremental else None,
            }
        )
    finally:
        connection.close()
    return status


def ensure_conversation(
    connection: sqlite3.Connection,
    conversation_id: str,
    observed_at: str,
    conversation_name: str = "",
) -> None:
    connection.execute(
        """
        INSERT INTO conversations(
          conversation_id,conversation_name,first_seen_at,last_seen_at,last_scan_at,
          last_complete_scan_at,last_scan_status,last_error_code,last_error_detail,
          last_message_at,visible_message_count
        ) VALUES(?,?,?,?,?,NULL,'event',NULL,NULL,NULL,0)
        ON CONFLICT(conversation_id) DO UPDATE SET
          conversation_name=CASE WHEN excluded.conversation_name<>'' THEN excluded.conversation_name ELSE conversations.conversation_name END,
          last_seen_at=excluded.last_seen_at
        """,
        (conversation_id, conversation_name, observed_at, observed_at, observed_at),
    )


def force_sensitive_reply(
    connection: sqlite3.Connection,
    conversation_id: str,
    create_time: str,
    current_text: str,
) -> bool:
    if not current_text.strip() or len(current_text.strip()) > 200:
        return False
    row = connection.execute(
        """
        SELECT text_redacted FROM messages
        WHERE conversation_id=? AND create_time<? AND current_visible=1
        ORDER BY create_time DESC LIMIT 1
        """,
        (conversation_id, create_time),
    ).fetchone()
    return bool(row and chat_sync.SENSITIVE_PROMPT_RE.search(str(row["text_redacted"] or "")))


def begin_auxiliary_run(connection: sqlite3.Connection, mode: str, started_at: str) -> str:
    run_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO sync_runs(run_id,started_at,status,mode) VALUES(?,?,'running',?)",
        (run_id, started_at, mode),
    )
    return run_id


def complete_auxiliary_run(
    connection: sqlite3.Connection,
    run_id: str,
    status: str,
    returned: int,
    inserted: int,
    updated: int,
    error_code: Optional[str] = None,
) -> None:
    connection.execute(
        """
        UPDATE sync_runs SET completed_at=?,status=?,messages_returned=?,messages_inserted=?,
          messages_updated=?,error_code=?,error_detail=? WHERE run_id=?
        """,
        (now_iso(), status, returned, inserted, updated, error_code, None, run_id),
    )


def event_message_payload(event: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    event_id = str(event.get("event_id") or "").strip()
    message_id = str(event.get("message_id") or "").strip()
    conversation_id = str(event.get("conversation_id") or "").strip()
    create_time = str(event.get("create_time") or event.get("event_time") or event.get("timestamp") or "").strip()
    if not event_id or not message_id or not conversation_id or not create_time:
        raise AutomationError("event_identity", "event identity is incomplete")
    content = event.get("content") if isinstance(event.get("content"), str) else ""
    raw = {
        "messageId": message_id,
        "conversationId": conversation_id,
        "createTime": create_time,
        "senderId": str(event.get("sender_open_dingtalk_id") or ""),
        "sender": str(event.get("sender") or ""),
        "text": content,
        "resourceRefs": [],
    }
    return event_id, message_id, conversation_id, raw


def ingest_event(config: dict[str, Any], event_kind: str, event: dict[str, Any]) -> dict[str, Any]:
    if event_kind not in ("direct", "group"):
        raise AutomationError("event_kind", "event kind must be direct or group")
    event_id, message_id, conversation_id, raw = event_message_payload(event)
    observed_at = now_iso()
    max_chars = int(config["dingtalk_chat"].get("max_text_chars", 200000))
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        inserted_event = connection.execute(
            """
            INSERT OR IGNORE INTO event_inbox(
              event_id,event_kind,message_id,conversation_id,event_time,received_at,state
            ) VALUES(?,?,?,?,?,?,'received')
            """,
            (
                event_id,
                event_kind,
                message_id,
                conversation_id,
                str(event.get("event_time") or event.get("timestamp") or ""),
                observed_at,
            ),
        ).rowcount
        if inserted_event == 0:
            connection.commit()
            return {"status": "duplicate", "queued": False}
        ensure_conversation(connection, conversation_id, observed_at)
        run_id = begin_auxiliary_run(connection, "event", observed_at)
        normalized = chat_sync.normalize_message(
            raw,
            conversation_id,
            max_chars,
            force_sensitive_reply(
                connection,
                conversation_id,
                str(raw["createTime"]),
                str(raw["text"]),
            ),
        )
        inserted, updated, change_kind = chat_sync.upsert_message(connection, normalized, observed_at, run_id)
        connection.execute(
            "UPDATE event_inbox SET state='staged',last_attempt_at=? WHERE event_id=?",
            (observed_at, event_id),
        )
        complete_auxiliary_run(connection, run_id, "success", 1, inserted, updated)
        connection.commit()
        status_update(config, last_event_at=observed_at, last_event_kind=event_kind)
        return {"status": "staged", "queued": bool(change_kind), "redactions": int(normalized["redaction_count"])}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def dws_binary(config: dict[str, Any]) -> Path:
    value = config["dingtalk_chat"].get("dws_bin") or _archive_path('/Users/example/.local/bin/dws')
    path = Path(str(value))
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AutomationError("dws_missing", "configured dws binary is unavailable")
    return path


def event_environment(config: dict[str, Any]) -> dict[str, str]:
    """Use one private socket root for the consumer, its bus child and status."""
    environment = {**os.environ, "LC_ALL": "en_US.UTF-8"}
    configured = config.get("dingtalk_automation", {}).get("event_runtime_root")
    if not configured:
        return environment
    root = Path(str(configured))
    endpoint = root / f"dws-event-{os.getuid()}" / ("dws-evt-" + "0" * 16 + ".sock")
    if not root.is_absolute() or len(os.fsencode(endpoint)) > 103:
        raise AutomationError("event_runtime_path_invalid", "event runtime path must be absolute and within the Unix socket limit")
    try:
        parent = root.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or not (
            (parent.st_uid == os.getuid() and parent.st_mode & 0o022 == 0) or parent.st_mode & stat.S_ISVTX
        ):
            raise ValueError("unsafe_parent")
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("unsafe_root")
    except (OSError, ValueError) as exc:
        raise AutomationError("event_runtime_directory_unsafe", "event runtime directory must be owned by this user with mode 0700") from exc
    environment["XDG_RUNTIME_DIR"] = str(root)
    return environment


def hydrate_pending_events(config: dict[str, Any], limit: int = 50) -> dict[str, Any]:
    automation = config["dingtalk_automation"]
    max_attempts = int(automation.get("event_max_hydration_attempts", 3))
    connection = connect_database(config)
    try:
        rows = connection.execute(
            """
            SELECT event_id,message_id FROM event_inbox
            WHERE state IN ('received','staged')
            ORDER BY received_at LIMIT ?
            """,
            (min(50, max(1, limit)),),
        ).fetchall()
        if not rows:
            blocked = connection.execute("SELECT COUNT(*) FROM event_inbox WHERE state='blocked'").fetchone()[0]
            return {"status": "noop", "requested": 0, "hydrated": 0, "blocked": int(blocked)}
        message_ids = list(dict.fromkeys(str(row["message_id"]) for row in rows))[:50]
        timeout = int(config["dingtalk_chat"].get("timeout_seconds", 180))
        payload, returncode, error_code, _error_detail = chat_sync.run_dws(
            dws_binary(config),
            ["chat", "+messages-mget", "--msg-ids", ",".join(message_ids), "--no-reactions"],
            timeout,
        )
        complete = (
            returncode == 0
            and payload.get("complete") is True
            and payload.get("partial") is not True
            and payload.get("hasMore") is not True
            and payload.get("truncated") is not True
            and int(payload.get("failedCount") or 0) == 0
            and isinstance(payload.get("messages"), list)
        )
        if not complete:
            connection.execute("BEGIN IMMEDIATE")
            for row in rows:
                current = connection.execute(
                    "SELECT attempt_count FROM event_inbox WHERE event_id=?", (row["event_id"],)
                ).fetchone()
                attempts = int(current["attempt_count"] or 0) + 1
                state = "blocked" if attempts >= max_attempts else "staged"
                connection.execute(
                    "UPDATE event_inbox SET state=?,attempt_count=?,last_attempt_at=?,last_error_code=? WHERE event_id=?",
                    (state, attempts, now_iso(), error_code or "messages_mget_incomplete", row["event_id"]),
                )
            connection.commit()
            blocked = connection.execute("SELECT COUNT(*) FROM event_inbox WHERE state='blocked'").fetchone()[0]
            return {"status": "partial", "requested": len(message_ids), "hydrated": 0, "blocked": int(blocked)}

        returned = payload.get("messages", [])
        returned_by_id = {
            str(item.get("messageId") or ""): item
            for item in returned
            if isinstance(item, dict) and str(item.get("messageId") or "")
        }
        observed_at = now_iso()
        max_chars = int(config["dingtalk_chat"].get("max_text_chars", 200000))
        connection.execute("BEGIN IMMEDIATE")
        run_id = begin_auxiliary_run(connection, "event", observed_at)
        inserted_total = 0
        updated_total = 0
        hydrated = 0
        blocked = 0
        for row in rows:
            raw = returned_by_id.get(str(row["message_id"]))
            if not raw:
                current = connection.execute(
                    "SELECT attempt_count FROM event_inbox WHERE event_id=?", (row["event_id"],)
                ).fetchone()
                attempts = int(current["attempt_count"] or 0) + 1
                state = "blocked" if attempts >= max_attempts else "staged"
                blocked += 1 if state == "blocked" else 0
                connection.execute(
                    "UPDATE event_inbox SET state=?,attempt_count=?,last_attempt_at=?,last_error_code='message_not_found' WHERE event_id=?",
                    (state, attempts, observed_at, row["event_id"]),
                )
                continue
            conversation_id = str(raw.get("conversationId") or "").strip()
            if not conversation_id:
                connection.execute(
                    "UPDATE event_inbox SET state='blocked',attempt_count=attempt_count+1,last_attempt_at=?,last_error_code='conversation_missing' WHERE event_id=?",
                    (observed_at, row["event_id"]),
                )
                blocked += 1
                continue
            ensure_conversation(connection, conversation_id, observed_at)
            raw_text = str(raw.get("text") or "")
            raw_time = str(raw.get("createTime") or "")
            normalized = chat_sync.normalize_message(
                raw,
                conversation_id,
                max_chars,
                force_sensitive_reply(connection, conversation_id, raw_time, raw_text),
            )
            inserted, updated, _change_kind = chat_sync.upsert_message(connection, normalized, observed_at, run_id)
            inserted_total += inserted
            updated_total += updated
            hydrated += 1
            connection.execute(
                "UPDATE event_inbox SET state='hydrated',attempt_count=attempt_count+1,last_attempt_at=?,last_error_code=NULL WHERE event_id=?",
                (observed_at, row["event_id"]),
            )
        complete_auxiliary_run(connection, run_id, "success", len(returned), inserted_total, updated_total)
        connection.commit()
        return {"status": "success", "requested": len(message_ids), "hydrated": hydrated, "blocked": blocked}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def sanitized_alert_title(value: str) -> str:
    text, _count = chat_sync.redact_text(value, 120)
    text = EMAIL_RE.sub("[邮箱已省略]", text)
    text = MOBILE_RE.sub("[手机号已省略]", text)
    return re.sub(r"[\r\n]+", " ", text).strip()[:120] or "自动化异常"


def notify_mac(title: str, body: str) -> bool:
    script = 'display notification ' + json.dumps(body, ensure_ascii=False) + ' with title ' + json.dumps(title, ensure_ascii=False)
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    return result.returncode == 0


def forward_sync_alert(config: dict[str, Any], kind: str, severity: str, error_code: str) -> dict[str, Any]:
    """Send only bounded fault metadata to the existing local alert owner."""
    if error_code == "validation_inputs_changed":
        return {"status": "deferred", "notified": False, "error_code": error_code}
    if (chat_read_recovery.scheduled_read_recovery(error_code)
            or error_code in {"chat_read_auth_required", "chat_read_blocked", "chat_checkpoint_invalid", "chat_checkpoint_busy"}):
        # The supervisor owns source alert severity, retry budget, holds and delivery.
        return {"status": "recorded_local", "notified": False, "error_code": error_code}
    try:
        live = json.loads(CONFIG_PATH.read_text())
        if any(config.get(key) != live.get(key) for key in ("external_root", "memory_root")):
            return {"status": "isolated", "notified": False}
        import business_sync_state as states
        import business_sync_supervisor as supervisor
        import local_sync_repair
        states.initialize()
        source, category = "dingtalk_chat", "sync_failure"
        if error_code.startswith("event_") or error_code.startswith("listener_"):
            category = "listener_failure"
        elif "validation" in error_code:
            source, category = "memory_core", "core_failure"
        elif kind != "failure":
            return {"status": "recorded_local", "notified": False}
        row = states.open_alert(source, category,
            "钉钉实时监听未完成验收" if category == "listener_failure" else "记忆库核心需要校验" if source == "memory_core" else "钉钉聊天处理失败",
            "失败记录与待处理消息已保留；自动处理和通知送达分别核验。",
            severity="error" if severity == "critical" else "warning", error_code=error_code)
        if source == "dingtalk_chat" and category == "sync_failure" and kind == "failure":
            if supervisor.hold_chat_failure(error_code) and not local_sync_repair.eligible(row):
                with states._connect() as connection:
                    connection.execute("UPDATE sync_alerts SET repair_status='blocked',repair_error_code=? "
                                       "WHERE alert_id=? AND repair_status='not_attempted'", (error_code, row["alert_id"]))
        if not local_sync_repair.maintenance_active():
            supervisor.deliver_due_alerts(config)
            supervisor.Supervisor(CONFIG_PATH).schedule_local_repair(config)
        return {"status": "handed_off", "notified": False, "alert_id": row["alert_id"]}
    except Exception as exc:
        append_log(runtime_paths(config)["log"], "alert_handoff_failed", error_code=type(exc).__name__)
        return {"status": "handoff_failed", "notified": False}


def raise_alert(
    config: dict[str, Any],
    kind: str,
    severity: str,
    dedupe_key: str,
    title: str,
    error_code: Optional[str] = None,
) -> dict[str, Any]:
    if error_code == "validation_inputs_changed":
        return {"status": "deferred", "notified": False, "error_code": error_code}
    automation = config["dingtalk_automation"]
    repeat_hours = int(automation.get("alert_repeat_hours", 6))
    timestamp = now_iso()
    safe_title = sanitized_alert_title(title)
    connection = connect_database(config)
    notify_due = False
    alert_id = str(uuid.uuid4())
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM automation_alerts WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        if existing:
            alert_id = str(existing["alert_id"])
            last_notified = str(existing["last_notified_at"] or "")
            notify_due = not last_notified
            if last_notified:
                try:
                    notify_due = datetime.fromisoformat(last_notified).astimezone() <= datetime.now().astimezone() - timedelta(hours=repeat_hours)
                except ValueError:
                    notify_due = True
            connection.execute(
                """
                UPDATE automation_alerts SET kind=?,severity=?,title=?,error_code=?,last_seen_at=?,
                  status='pending',codex_status=CASE WHEN codex_status='recorded' THEN 'pending' ELSE codex_status END,
                  resolved_at=NULL WHERE alert_id=?
                """,
                (kind, severity, safe_title, error_code, timestamp, alert_id),
            )
        else:
            notify_due = True
            connection.execute(
                """
                INSERT INTO automation_alerts(
                  alert_id,dedupe_key,kind,severity,title,error_code,created_at,last_seen_at,
                  status,codex_status
                ) VALUES(?,?,?,?,?,?,?,?, 'pending','pending')
                """,
                (alert_id, dedupe_key, kind, severity, safe_title, error_code, timestamp, timestamp),
            )
        connection.commit()
    finally:
        connection.close()

    handoff = ({"status": "recorded_local"} if dedupe_key == "incremental-partial"
               else forward_sync_alert(config, kind, severity, error_code or "unknown_failure"))
    return {"alert_id": alert_id, "notified": False, "title": safe_title, "notification_handoff": handoff["status"]}


def resolve_alert(config: dict[str, Any], dedupe_key: str) -> None:
    connection = connect_database(config)
    try:
        connection.execute(
            "UPDATE automation_alerts SET status='resolved',resolved_at=? WHERE dedupe_key=? AND status!='resolved'",
            (now_iso(), dedupe_key),
        )
        connection.commit()
    finally:
        connection.close()


def find_retry_metadata(value: Any) -> tuple[Optional[bool], int]:
    flags: list[bool] = []
    retry_after = 0
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if isinstance(item.get("retryable"), bool):
                flags.append(bool(item["retryable"]))
            for key in ("retry_after_seconds", "retryAfterSeconds"):
                try:
                    retry_after = max(retry_after, int(item.get(key) or 0))
                except (TypeError, ValueError):
                    pass
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return (False if False in flags else True if flags else None), retry_after


def structured_event_errors(lines: list[str]) -> list[dict[str, Any]]:
    """Accept both pretty JSON and NDJSON on stderr; never persist raw output."""
    text = "\n".join(lines)
    decoder = json.JSONDecoder()
    parsed = []
    for match in re.finditer(r"(?:^|\n)\s*(?=\{)", text):
        try:
            item, _ = decoder.raw_decode(text[match.end():])
            if isinstance(item, dict):
                parsed.append(item)
        except ValueError:
            pass
    return parsed


def safe_event_failure(lines: list[str]) -> dict[str, Any]:
    """Keep codes and retry controls, never stderr prose, tokens or identities."""
    parsed = structured_event_errors(lines)
    retryable, retry_after = find_retry_metadata(parsed)
    codes = set()
    if "transport: socket runtime root is neither private nor sticky:" in "\n".join(lines):
        codes.add("event_socket_runtime_root_unsafe")
    # Recognize the public, credential-free local failure before the CLI can
    # create any subscription. Raw OAuth stderr remains private.
    if any(re.fullmatch(r"(?:● )?Error: event consume --as user: "
                        r"(?:edition token provider: )?(?:load access token: |load OAuth identity metadata: )"
                        r"load from keychain: read DEK from macOS Keychain: context deadline exceeded", line)
           for line in lines):
        codes.add("event_oauth_keychain_read_timeout")
    stack = list(parsed)
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"code", "reason", "error_code"} and isinstance(item, str) and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,79}", item):
                    codes.add(item)
                elif isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(value, list):
            stack.extend(value)
    return {"error_codes": sorted(codes), "retryable": retryable, "retry_after_seconds": retry_after,
            "structured_error_seen": bool(parsed), "stderr_sha256": sha256_text("\n".join(lines))}


class EventConsumer:
    def __init__(
        self,
        config: dict[str, Any],
        kind: str,
        event_lines: "queue.Queue[tuple[str, str]]",
        stop_event: threading.Event,
    ) -> None:
        self.config = config
        self.kind = kind
        self.event_kind = "direct" if kind == "all-direct" else "group"
        self.event_lines = event_lines
        self.stop_event = stop_event
        self.process: Optional[subprocess.Popen[str]] = None
        self.ready = threading.Event()
        self.subscription_ref: Optional[str] = None
        self.stderr_lines: list[str] = []
        self.pre_ready: list[str] = []
        self.threads: list[threading.Thread] = []
        identity = sha256_text(json.dumps([str(dws_binary(config)), config["dingtalk_automation"].get("scope"),
                                          config.get("dingtalk_chat", {}).get("profile", "default"), kind]))[:24]
        self.budget = ListenerBudget(runtime_paths(config)["status"].parent / ("listener-budget-" + identity + ".json"))

    def _stderr_reader(self) -> None:
        process = self.process
        if not process or not process.stderr:
            return
        for line in process.stderr:
            text = line.rstrip("\r\n")
            if len(self.stderr_lines) < 200:
                self.stderr_lines.append(text[:2000])
            if "[event] ready " in text:
                match = re.search(r"\bsubscribe_id=([^\s]+)", text)
                self.subscription_ref = sha256_text(match.group(1)) if match else None
                self.ready.set()
                buffered = list(self.pre_ready)
                self.pre_ready.clear()
                for pending in buffered:
                    self.event_lines.put((self.event_kind, pending))

    def _stdout_reader(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return
        for line in process.stdout:
            text = line.strip()
            if not text:
                continue
            if self.ready.is_set():
                self.event_lines.put((self.event_kind, text))
            elif len(self.pre_ready) < 100:
                self.pre_ready.append(text)

    def _spawn(self) -> None:
        command = [
            str(dws_binary(self.config)),
            "event",
            "+listen-im",
            "--kind",
            self.kind,
            "--format",
            "ndjson",
        ]
        if getattr(self, "diagnostic_verbose", False):
            command.append("--verbose")
        self.ready.clear()
        self.stderr_lines.clear()
        self.pre_ready.clear()
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=event_environment(self.config),
        )
        self.threads = [
            threading.Thread(target=self._stdout_reader, name=f"dws-{self.event_kind}-stdout", daemon=True),
            threading.Thread(target=self._stderr_reader, name=f"dws-{self.event_kind}-stderr", daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def _retry_info(self) -> tuple[Optional[bool], int]:
        return find_retry_metadata(structured_event_errors(self.stderr_lines + self.pre_ready))

    def start_with_budget(self) -> None:
        # Local configuration errors must not consume a provider attempt.
        event_environment(self.config)
        ready_timeout = int(self.config["dingtalk_automation"].get("event_ready_timeout_seconds", 60))
        while not self.stop_event.is_set():
            try:
                self.budget.reserve()
            except BudgetBlocked as exc:
                raise AutomationError(exc.code, "listener start blocked by persistent governed budget") from exc
            self._spawn()
            deadline = time.monotonic() + ready_timeout
            while time.monotonic() < deadline and not self.stop_event.is_set():
                if self.ready.wait(timeout=0.25):
                    self.budget.ready()
                    return
                if self.process and self.process.poll() is not None:
                    for thread in self.threads:
                        thread.join(timeout=1)
                    break
            retryable, retry_after = self._retry_info()
            failure = safe_event_failure(self.stderr_lines + self.pre_ready)
            from listener_diagnostic import sanitized_stderr
            failure.update(recorded_at=now_iso(), kind=self.event_kind,
                           phase="process_exit_before_ready" if self.process and self.process.poll() is not None else "ready_timeout",
                           ready_timeout_seconds=ready_timeout, stderr_line_count=len(self.stderr_lines),
                           ready_wait_expired=bool(self.process and self.process.poll() is None),
                           stdout_pre_ready_lines_seen=len(self.pre_ready),
                           sanitized_stderr=sanitized_stderr(self.stderr_lines),
                           process_exit_code=self.process.poll() if self.process else None)
            batch_contract.save(runtime_paths(self.config)["status"].parent / ("listener-last-failure-" + self.event_kind + ".json"), failure)
            guard, next_retry_at = None, None
            for structured in structured_event_errors(self.stderr_lines + self.pre_ready):
                stack = [structured]
                while stack:
                    item = stack.pop()
                    if isinstance(item, dict):
                        if budget_time(item.get("next_retry_at")):
                            candidate = item["next_retry_at"]
                            if not next_retry_at or budget_time(candidate) > budget_time(next_retry_at):
                                next_retry_at = candidate
                        for value in item.values():
                            if isinstance(value, str) and value in {"in_flight", "cooldown", "terminal_hold"}:
                                guard = value
                        stack.extend(item.values())
                    elif isinstance(item, list):
                        stack.extend(item)
            budget = self.budget.failed(retryable, retry_after=retry_after, next_retry_at=next_retry_at, guard=guard,
                                        code=failure["error_codes"][0] if failure["error_codes"] else "event_not_ready")
            self.shutdown()
            if guard or budget["attempt_count"] >= budget["max_attempts"]:
                raise AutomationError(
                    "event_subscription_" + guard if guard else "event_retry_budget_exhausted",
                    f"{self.event_kind} listener did not reach ready within the governed retry budget",
                )
            wait_seconds = max(1, (budget_time(budget["next_retry_at"]) - datetime.now().astimezone()).total_seconds())
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline and not self.stop_event.wait(timeout=min(5.0, deadline - time.monotonic())):
                pass
        raise AutomationError("listener_stopped", "listener stopped before ready")

    def shutdown(self) -> None:
        process = self.process
        if not process:
            return
        if process.poll() is None and process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                # Deliberately do not SIGKILL; the DWS contract requires graceful cleanup.
                pass


def listener_event_worker(
    config: dict[str, Any],
    event_lines: "queue.Queue[tuple[str, str]]",
    stop_event: threading.Event,
) -> None:
    paths = runtime_paths(config)
    malformed = 0
    while not stop_event.is_set() or not event_lines.empty():
        try:
            event_kind, line = event_lines.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("event line is not an object")
            result = ingest_event(config, event_kind, payload)
            if result.get("status") == "staged":
                current_status = json.loads(paths["status"].read_text()) if paths["status"].is_file() else {}
                if current_status.get("live_acceptance") != "passed":
                    update_live_acceptance(config)
            append_log(
                paths["log"],
                "event_ingested",
                event_kind=event_kind,
                status=result["status"],
                queued=result["queued"],
                redactions=int(result.get("redactions") or 0),
            )
        except (json.JSONDecodeError, ValueError, AutomationError, chat_sync.SyncError) as exc:
            malformed += 1
            code = exc.code if isinstance(exc, (AutomationError, chat_sync.SyncError)) else "event_invalid_json"
            append_log(paths["log"], "event_rejected", event_kind=event_kind, error_code=code)
            if malformed == 10:
                raise_alert(config, "failure", "warning", "event-malformed-lines", "连续收到无法解析的钉钉事件", code)
        except Exception as exc:
            append_log(paths["log"], "event_worker_failed", event_kind=event_kind, error_code=type(exc).__name__)
            raise_alert(config, "failure", "critical", "event-worker-failed", "钉钉事件写入器失败", type(exc).__name__)
        finally:
            event_lines.task_done()


def hydration_worker(config: dict[str, Any], stop_event: threading.Event) -> None:
    interval = int(config["dingtalk_automation"].get("event_hydration_interval_seconds", 15))
    paths = runtime_paths(config)
    while not stop_event.is_set():
        try:
            result = hydrate_pending_events(config)
            if result["status"] != "noop":
                append_log(
                    paths["log"],
                    "event_hydration",
                    status=result["status"],
                    requested=result["requested"],
                    hydrated=result["hydrated"],
                    blocked=result["blocked"],
                )
            if int(result.get("blocked") or 0) > 0:
                raise_alert(
                    config,
                    "failure",
                    "warning",
                    "event-hydration-blocked",
                    "部分钉钉事件详情连续补取失败，等待整点对账恢复",
                    "event_hydration_blocked",
                )
            else:
                resolve_alert(config, "event-hydration-blocked")
        except Exception as exc:
            append_log(paths["log"], "event_hydration_failed", error_code=type(exc).__name__)
        stop_event.wait(max(1, interval))


def listener_admission(consumers):
    from listener_retry_budget import parse_time
    from listener_manual_acceptance import OperationalBudget
    for consumer in consumers:
        row = consumer.budget.read()
        now = datetime.now().astimezone()
        if row.get("state") == "ready" and isinstance(consumer.budget, OperationalBudget):
            try:
                consumer.budget.check_ready_episode(row)
            except BudgetBlocked as exc:
                return exc.code
            continue
        if row.get("state") == "stopped" and not isinstance(consumer.budget, __import__("listener_manual_acceptance").ManualBudget):
            continue
        due = parse_time(row.get("next_retry_at"))
        if row.get("guard") and (due is None or now < due):
            return "event_subscription_" + str(row["guard"])
        if due and now < due:
            return "event_subscription_cooldown"
        if int(row.get("attempt_count", 0)) >= int(row.get("max_attempts", 1)):
            return "event_retry_budget_exhausted"
    return None


def run_listener(config: dict[str, Any]) -> int:
    lock = acquire_lock(LISTENER_LOCK)
    stop_event = threading.Event()
    event_lines: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=1000)
    consumers = [
        EventConsumer(config, "all-direct", event_lines, stop_event),
        EventConsumer(config, "all-group", event_lines, stop_event),
    ]
    stopped_by_signal = False

    def stop_handler(_signum: int, _frame: Any) -> None:
        nonlocal stopped_by_signal
        stopped_by_signal = True
        stop_event.set()

    old_term = signal.signal(signal.SIGTERM, stop_handler)
    old_int = signal.signal(signal.SIGINT, stop_handler)
    worker: Optional[threading.Thread] = None
    hydrator: Optional[threading.Thread] = None
    paths = runtime_paths(config)
    manual_receipt = None
    admission_blocked = False
    try:
        import listener_manual_acceptance as manual_listener
        manual_receipt = manual_listener.take(config)
        if manual_receipt:
            for consumer in consumers:
                consumer.budget = manual_listener.ManualBudget(config, consumer.kind, manual_receipt)
        else:
            operational = manual_listener.operational_budgets(config)
            if operational:
                for consumer in consumers:
                    consumer.budget = operational[consumer.kind]
        blocked = listener_admission(consumers)
        if blocked:
            prior = json.loads(paths["status"].read_text()) if paths["status"].is_file() else {}
            if prior.get("listener_state") != "failed" or prior.get("listener_error_code") != blocked:
                status_update(config, listener_state="failed", listener_error_code=blocked,
                              direct_ready=False, group_ready=False, live_acceptance="live_acceptance_pending")
                append_log(paths["log"], "listener_admission_blocked", error_code=blocked)
            admission_blocked = True
            return 0
        status_update(
            config,
            listener_state="starting",
            listener_pid=os.getpid(),
            direct_ready=False,
            group_ready=False,
            listener_started_at=now_iso(),
            live_acceptance="live_acceptance_pending",
            incoming_event_seen=False,
            subscriptions_valid=False,
            live_subscription_receipts={},
            live_event_receipts={},
            listener_restart_policy="bounded_automatic",
        )
        consumers[0].start_with_budget()
        status_update(config, direct_ready=True, direct_subscription_ref=consumers[0].subscription_ref)
        consumers[1].start_with_budget()
        status_update(config, group_ready=True, listener_state="ready", listener_ready_at=now_iso(),
                      group_subscription_ref=consumers[1].subscription_ref, listener_error_code=None)
        if manual_receipt:
            manual_listener.finish(config, manual_receipt, "ready_acceptance_pending")
        append_log(paths["log"], "listener_ready", direct_ready=True, group_ready=True)
        worker = threading.Thread(target=listener_event_worker, args=(config, event_lines, stop_event), daemon=True)
        hydrator = threading.Thread(target=hydration_worker, args=(config, stop_event), daemon=True)
        worker.start()
        hydrator.start()
        while not stop_event.wait(timeout=1):
            for consumer in consumers:
                if consumer.process and consumer.process.poll() is not None:
                    raise AutomationError("event_process_exited", f"{consumer.event_kind} event consumer exited")
        return 0
    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__))
        if manual_receipt:
            manual_listener.finish(config, manual_receipt, "failed", code)
        if not stopped_by_signal:
            status_update(config, listener_error_code=code, live_acceptance="live_acceptance_pending")
            raise_alert(config, "failure", "critical", "listener-not-ready", "钉钉聊天监听未保持就绪", code)
        append_log(paths["log"], "listener_failed", error_code=code)
        # Keep a failed state, but let launchd stop when restarting cannot help.
        parked = (code == "event_retry_budget_exhausted"
                  or isinstance(exc, BudgetBlocked)
                  or code.startswith(("event_budget_", "event_accepted_", "event_operational_"))
                  or code in {"event_subscription_terminal_hold", "event_subscription_in_flight",
                              "event_listener_owner_alive", "event_ready_episode_unverified"})
        if parked:
            status_update(config, listener_restart_policy="manual_required")
        return 0 if parked else 1
    finally:
        stop_event.set()
        for consumer in reversed(consumers):
            consumer.shutdown()
            if stopped_by_signal:
                consumer.budget.stopped()
        if worker:
            worker.join(timeout=5)
        if hydrator:
            hydrator.join(timeout=5)
        if not admission_blocked:
            status_update(
                config,
                listener_state="stopped" if stopped_by_signal else "failed",
                direct_ready=False,
                group_ready=False,
                listener_stopped_at=now_iso(),
            )
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        lock.close()


def yaml_scalar_from_raw(raw: str) -> Any:
    value = raw.strip()
    if value == "null":
        return None
    if value in ("true", "false"):
        return value == "true"
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def frontmatter_scalar(raw_frontmatter: str, key: str) -> Any:
    match = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", raw_frontmatter, re.MULTILINE)
    return yaml_scalar_from_raw(match.group(1)) if match else None


def parse_memory_note(path: Path, strict: bool = False) -> Optional[dict[str, Any]]:
    text: Optional[str] = None
    for attempt in range(3):
        try:
            text = path.read_text(encoding="utf-8")
            break
        except UnicodeDecodeError as exc:
            if strict:
                raise AutomationError("memory_source_encoding", "a governed memory note is not valid UTF-8") from exc
            return None
        except OSError as exc:
            if attempt >= 2:
                if strict:
                    raise AutomationError("memory_source_unreadable", "a governed memory note cannot be read") from exc
                return None
            time.sleep(attempt + 1)
    if text is None:
        return None
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None
    raw = match.group(1)
    body = match.group(2)
    memory_id = str(frontmatter_scalar(raw, "memory_id") or "")
    if not MEMORY_ID_RE.match(memory_id):
        return None
    section = ""
    for heading in ("记忆内容", "对话摘要", "当前状态"):
        section_match = re.search(rf"^## {heading}[ \t]*\n+(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
        if section_match and section_match.group(1).strip():
            section = re.sub(r"\s+", " ", section_match.group(1).strip())[:600]
            break
    return {
        "path": path,
        "text": text,
        "raw_frontmatter": raw,
        "body": body,
        "memory_id": memory_id,
        "category": str(frontmatter_scalar(raw, "category") or ""),
        "memory_scope": str(frontmatter_scalar(raw, "memory_scope") or ""),
        "memory_kind": str(frontmatter_scalar(raw, "memory_kind") or ""),
        "status": str(frontmatter_scalar(raw, "status") or ""),
        "fact_status": str(frontmatter_scalar(raw, "fact_status") or ""),
        "source_check": str(frontmatter_scalar(raw, "source_check") or ""),
        "source_system": str(frontmatter_scalar(raw, "source_system") or ""),
        "source_ref": str(frontmatter_scalar(raw, "source_ref") or ""),
        "observed_at": str(frontmatter_scalar(raw, "observed_at") or ""),
        "title": path.stem,
        "summary": section,
    }


def memory_records(memory_root: Path, strict: bool = True) -> list[dict[str, Any]]:
    directories = ["01_关键业务认知", "02_内部业务资料", "03_外部沟通", "04_对话来源摘要", "90_待确认"]
    records: list[dict[str, Any]] = []
    for directory in directories:
        source_directory = memory_root / directory
        try:
            paths = sorted(source_directory.glob("**/*.md"))
        except OSError as exc:
            raise AutomationError("memory_root_unavailable", "business memory directory cannot be enumerated") from exc
        for path in paths:
            if path.name == "00_待确认总表.md":
                continue
            record = parse_memory_note(path, strict=strict)
            if record:
                records.append(record)
    if strict and not records:
        raise AutomationError("memory_context_empty", "business memory context is unexpectedly empty")
    return records


def model_memory_context(memory_root: Path) -> list[dict[str, str]]:
    return [
        {
            "memory_id": str(record["memory_id"]),
            "title": str(record["title"]),
            "category": str(record["category"]),
            "memory_kind": str(record["memory_kind"]),
            "status": str(record["status"]),
            "fact_status": str(record["fact_status"]),
            "source_check": str(record["source_check"]),
            "summary": str(record["summary"]),
        }
        for record in memory_records(memory_root)
    ]


def recover_stale_batches(config: dict[str, Any]) -> int:
    threshold = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
    connection = connect_database(config)
    try:
        rows = connection.execute(
            """
            SELECT batch_id FROM semantic_batches
            WHERE status IN ('prepared','model_complete') AND created_at<?
            """,
            (threshold,),
        ).fetchall()
        if not rows:
            return 0
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            batch_id = str(row["batch_id"])
            connection.execute(
                "UPDATE semantic_queue SET state='failed',last_error_code='stale_batch',batch_id=NULL WHERE batch_id=? AND state='claimed'",
                (batch_id,),
            )
            connection.execute(
                "UPDATE semantic_batches SET status='failed',completed_at=?,error_code='stale_batch' WHERE batch_id=?",
                (now_iso(), batch_id),
            )
        connection.commit()
        return len(rows)
    finally:
        connection.close()


def context_messages(
    connection: sqlite3.Connection,
    conversation_id: str,
    before_time: str,
    excluded_ids: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT message_id,create_time,sender_name,text_redacted FROM messages
        WHERE conversation_id=? AND create_time<? AND current_visible=1
        ORDER BY create_time DESC LIMIT ?
        """,
        (conversation_id, before_time, limit * 2),
    ).fetchall()
    result = []
    for row in reversed(rows):
        if str(row["message_id"]) in excluded_ids:
            continue
        result.append(
            {
                "time": str(row["create_time"]),
                "sender": str(row["sender_name"]),
                "text": str(row["text_redacted"]),
                "context_only": True,
            }
        )
        if len(result) >= limit:
            break
    return result


def select_batch_rows(
    connection: sqlite3.Connection,
    max_messages: int,
    max_chars: int,
) -> tuple[list[sqlite3.Row], set[int]]:
    candidates = connection.execute(
        """
        SELECT q.queue_id,q.message_id,q.content_sha256,q.change_kind,q.queued_at,
               m.conversation_id,m.create_time,m.sender_name,m.text_redacted,m.current_visible,
               c.conversation_name
        FROM semantic_queue q
        JOIN messages m ON m.message_id=q.message_id
        JOIN conversations c ON c.conversation_id=m.conversation_id
        WHERE q.state IN ('pending','failed','deferred')
          AND COALESCE(q.last_error_code,'')!='recovery_conflict_requires_review'
          AND NOT EXISTS (SELECT 1 FROM batch_messages frozen_link
            JOIN semantic_batches frozen ON frozen.batch_id=frozen_link.batch_id
            WHERE frozen_link.queue_id=q.queue_id AND frozen.proposal_sha256 IS NOT NULL
              AND frozen.status IN ('failed','model_complete'))
          AND NOT EXISTS (
            SELECT 1 FROM batch_messages conflict_link
            JOIN semantic_recovery_holds conflict_hold ON conflict_hold.batch_id=conflict_link.batch_id
            JOIN semantic_queue conflict_queue ON conflict_queue.queue_id=conflict_link.queue_id
            WHERE conflict_queue.message_id=q.message_id AND conflict_queue.content_sha256=q.content_sha256
          )
          AND NOT EXISTS (
            SELECT 1 FROM batch_messages blocked_link
            JOIN semantic_batches blocked_batch ON blocked_batch.batch_id=blocked_link.batch_id
            JOIN semantic_queue blocked_queue ON blocked_queue.queue_id=blocked_link.queue_id
            WHERE blocked_batch.error_code='recovery_conflict_requires_review'
              AND blocked_queue.message_id=q.message_id
              AND NOT EXISTS (SELECT 1 FROM recovery_resolutions resolution WHERE resolution.old_batch_id=blocked_batch.batch_id AND resolution.state='verified')
          )
        ORDER BY q.queued_at,q.queue_id LIMIT ?
        """,
        (max_messages + 1,),
    ).fetchall()
    selected: list[sqlite3.Row] = []
    truncated: set[int] = set()
    used = 0
    for row in candidates:
        if len(selected) >= max_messages:
            break
        length = len(str(row["text_redacted"]))
        if selected and used + length > max_chars:
            break
        selected.append(row)
        if len(selected) == 1 and length > max_chars:
            truncated.add(int(row["queue_id"]))
            used = max_chars
            break
        used += length
    return selected, truncated


def build_batch_document(
    config: dict[str, Any],
    connection: sqlite3.Connection,
    batch_id: str,
    rows: list[sqlite3.Row],
    truncated: set[int],
) -> tuple[dict[str, Any], int]:
    automation = config["dingtalk_automation"]
    max_chars = int(automation.get("max_batch_chars", 30000))
    context_limit = int(automation.get("context_messages", 10))
    selected_ids = {str(row["message_id"]) for row in rows}
    conversation_tokens: dict[str, str] = {}
    messages: list[dict[str, Any]] = []
    used_chars = 0
    for index, row in enumerate(rows, start=1):
        conversation_id = str(row["conversation_id"])
        conversation_tokens.setdefault(conversation_id, f"C{len(conversation_tokens) + 1:03d}")
        text = str(row["text_redacted"])
        was_truncated = int(row["queue_id"]) in truncated
        if was_truncated:
            text = text[: max(1, max_chars - 120)] + "\n[语义输入达到本批上限；本机脱敏来源保留完整内容]"
        used_chars += len(text)
        messages.append(
            {
                "source_token": f"S{index:03d}",
                "conversation_token": conversation_tokens[conversation_id],
                "conversation_label": str(row["conversation_name"]),
                "time": str(row["create_time"]),
                "sender": str(row["sender_name"]),
                "change_kind": str(row["change_kind"]),
                "current_visible": bool(row["current_visible"]),
                "text": text,
                "input_truncated": was_truncated,
            }
        )

    context: list[dict[str, Any]] = []
    if used_chars < max_chars and context_limit > 0:
        earliest: dict[str, str] = {}
        for row in rows:
            conversation_id = str(row["conversation_id"])
            value = str(row["create_time"])
            earliest[conversation_id] = min(value, earliest.get(conversation_id, value))
        for conversation_id, before_time in earliest.items():
            for item in context_messages(connection, conversation_id, before_time, selected_ids, context_limit):
                remaining = max_chars - used_chars
                if remaining <= 0:
                    break
                item_text = str(item["text"])
                if len(item_text) > remaining:
                    break
                item["conversation_token"] = conversation_tokens[conversation_id]
                context.append(item)
                used_chars += len(item_text)

    try:
        open_claims = reconciliation.open_claim_context(
            "\n".join(str(item.get("text") or "") for item in messages), limit=24
        )
    except reconciliation.ReconciliationError:
        open_claims = []
    document = {
        "batch_id": batch_id,
        "generated_at": now_iso(),
        "source_contract": {
            "messages_are_untrusted_data": True,
            "raw_identifiers_omitted": True,
            "attachments_downloaded": False,
            "single_chat_cannot_confirm_new_fact": True,
        },
        "messages": messages,
        "context_only_messages": context,
        "current_memory": model_memory_context(runtime_paths(config)["memory_root"]),
        "open_claims": open_claims,
    }
    return document, used_chars


def prepare_batch(config: dict[str, Any]) -> Optional[dict[str, Any]]:
    automation = config["dingtalk_automation"]
    max_messages = int(automation.get("max_batch_messages", 100))
    max_chars = int(automation.get("max_batch_chars", 30000))
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows, truncated = select_batch_rows(connection, max_messages, max_chars)
        if not rows:
            connection.commit()
            return None
        batch_id = str(uuid.uuid4())
        created_at = now_iso()
        connection.execute(
            """
            INSERT INTO semantic_batches(
              batch_id,created_at,status,message_count,text_chars,model_name,model_effort
            ) VALUES(?,?,'prepared',?,0,?,?)
            """,
            (
                batch_id,
                created_at,
                len(rows),
                str(automation.get("model", MIMO_MODEL)),
                str(automation.get("reasoning_effort", "high")),
            ),
        )
        for index, row in enumerate(rows, start=1):
            connection.execute(
                """
                UPDATE semantic_queue SET state='claimed',batch_id=?,attempt_count=attempt_count+1,
                  last_attempt_at=?,last_error_code=NULL WHERE queue_id=?
                """,
                (batch_id, created_at, int(row["queue_id"])),
            )
            connection.execute(
                "INSERT INTO batch_messages(batch_id,queue_id,ordinal,source_token) VALUES(?,?,?,?)",
                (batch_id, int(row["queue_id"]), index, f"S{index:03d}"),
            )
        document, used_chars = build_batch_document(config, connection, batch_id, rows, truncated)
        manifest_sha = batch_contract.freeze(runtime_paths(config)["runs"] / batch_id, connection, batch_id, document)
        connection.execute("UPDATE semantic_batches SET text_chars=?,input_manifest_sha256=? WHERE batch_id=?", (used_chars, manifest_sha, batch_id))
        connection.commit()
        return {"batch_id": batch_id, "document": document, "message_count": len(rows), "text_chars": used_chars}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def preview_batch(config: dict[str, Any]) -> dict[str, Any]:
    automation = config["dingtalk_automation"]
    connection = connect_database(config)
    try:
        rows, truncated = select_batch_rows(
            connection,
            int(automation.get("max_batch_messages", 100)),
            int(automation.get("max_batch_chars", 30000)),
        )
        return {
            "status": "dry_run",
            "would_process_messages": len(rows),
            "would_truncate_inputs": len(truncated),
            "would_call_model": bool(rows),
            "would_write_memory": False,
        }
    finally:
        connection.close()


def sanitize_note_text(value: Any, max_chars: int) -> str:
    text = value if isinstance(value, str) else ""
    text, _count = chat_sync.redact_text(text, max_chars)
    text = EMAIL_RE.sub("[邮箱已省略]", text)
    text = MOBILE_RE.sub("[手机号已省略]", text)
    text = text.replace("\x00", "")
    if SECRET_RE.search(text):
        raise AutomationError("proposal_secret", "model proposal contains a secret-like value")
    if UNSAFE_MARKDOWN_RE.search(text):
        raise AutomationError("proposal_markup", "model proposal contains unsafe structural markup")
    return text.strip()


def proposal_tokens(config: dict[str, Any], batch_id: str) -> set[str]:
    connection = connect_database(config)
    try:
        return {
            str(row["source_token"])
            for row in connection.execute("SELECT source_token FROM batch_messages WHERE batch_id=?", (batch_id,))
        }
    finally:
        connection.close()


def validate_proposal(config: dict[str, Any], batch_id: str, payload: Any, *, check_context: bool = True) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("batch_id") != batch_id:
        raise AutomationError("proposal_batch", "model proposal batch ID does not match")
    allowed_tokens = proposal_tokens(config, batch_id)
    source_summary = payload.get("source_summary")
    if source_summary is not None:
        if not isinstance(source_summary, dict):
            raise AutomationError("proposal_shape", "source_summary must be an object or null")
        source_summary = {
            "title": sanitize_note_text(source_summary.get("title"), 100),
            "summary": sanitize_note_text(source_summary.get("summary"), 1600),
            "evidence_boundary": sanitize_note_text(source_summary.get("evidence_boundary"), 1000),
            "source_tokens": [str(token) for token in source_summary.get("source_tokens", [])],
        }
        if not source_summary["summary"] or not source_summary["evidence_boundary"]:
            raise AutomationError("proposal_shape", "source summary text is empty")
        if not source_summary["title"]:
            raise AutomationError("proposal_shape", "source summary title is empty")
        if not source_summary["source_tokens"] or not set(source_summary["source_tokens"]).issubset(allowed_tokens):
            raise AutomationError("proposal_source", "source summary references an unknown token")
        if len(source_summary["source_tokens"]) != len(set(source_summary["source_tokens"])):
            raise AutomationError("proposal_source", "source summary repeats a source token")

    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) > 20:
        raise AutomationError("proposal_shape", "candidates must be an array with at most 20 items")
    candidates = []
    seen_dedupe_keys: set[str] = set()
    allowed_actions = {"annotate_page", "link_existing", "conflict"}
    allowed_categories = {"business_cognition", "internal_material", "external_communication"}
    allowed_kinds = {"fact", "judgment", "decision", "status", "open_question"}
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            raise AutomationError("proposal_shape", "candidate must be an object")
        action = str(raw.get("action") or "")
        category = str(raw.get("category") or "")
        memory_kind = str(raw.get("memory_kind") or "")
        if action not in allowed_actions or category not in allowed_categories or memory_kind not in allowed_kinds:
            raise AutomationError("proposal_enum", "candidate contains an unsupported action or memory type")
        tokens = [str(token) for token in raw.get("source_tokens", [])]
        if not tokens or not set(tokens).issubset(allowed_tokens):
            raise AutomationError("proposal_source", "candidate references an unknown source token")
        if len(tokens) != len(set(tokens)):
            raise AutomationError("proposal_source", "candidate repeats a source token")
        existing_id = raw.get("existing_memory_id")
        existing_id = str(existing_id) if isinstance(existing_id, str) and existing_id else None
        if action in {"link_existing", "conflict"} and not existing_id:
            raise AutomationError("proposal_existing", f"{action} requires an existing memory ID")
        if action == "annotate_page" and existing_id:
            raise AutomationError("proposal_existing", "annotate_page may not claim an existing memory match")
        if existing_id and not MEMORY_ID_RE.match(existing_id):
            raise AutomationError("proposal_existing", "existing memory ID is invalid")
        candidate = {
            "action": action,
            "dedupe_key": sanitize_note_text(raw.get("dedupe_key"), 120),
            "title": sanitize_note_text(raw.get("title"), 100),
            "category": category,
            "memory_kind": memory_kind,
            "summary": sanitize_note_text(raw.get("summary"), 1800),
            "evidence_boundary": sanitize_note_text(raw.get("evidence_boundary"), 1200),
            "source_tokens": tokens,
            "existing_memory_id": existing_id,
            "conflict_reason": sanitize_note_text(raw.get("conflict_reason"), 800) if raw.get("conflict_reason") else None,
        }
        if not candidate["dedupe_key"] or not candidate["title"] or not candidate["summary"] or not candidate["evidence_boundary"]:
            raise AutomationError("proposal_shape", "candidate contains an empty required field")
        if candidate["dedupe_key"] in seen_dedupe_keys:
            raise AutomationError("proposal_duplicate", "proposal contains a duplicate semantic key")
        seen_dedupe_keys.add(candidate["dedupe_key"])
        if action == "conflict" and not candidate["conflict_reason"]:
            raise AutomationError("proposal_shape", "conflict candidate requires conflict_reason")
        candidates.append(candidate)
    no_business_change = payload.get("no_business_change") is True
    if no_business_change and (source_summary is not None or candidates):
        raise AutomationError("proposal_shape", "no_business_change cannot accompany memory writes")
    if not no_business_change and source_summary is None and not candidates:
        raise AutomationError("proposal_shape", "proposal must contain a source summary, candidates, or no_business_change")
    raw_relations = payload.get("claim_relations", [])
    try:
        connection = connect_database(config)
        try:
            manifest = batch_contract.load(runtime_paths(config)["runs"] / batch_id, connection, batch_id)
        finally:
            connection.close()
        document = manifest["document"] if manifest else None
        handles = batch_contract.check_relations(raw_relations, document, allowed_tokens)
        selected_handles = {str(item["claim_handle"]) for item in raw_relations}
        if selected_handles and check_context:
            frozen = {str(row["claim_handle"]): row.get("context_fingerprint")
                      for row in document.get("open_claims", []) if row["claim_handle"] in selected_handles}
            if reconciliation.claim_context_hashes(selected_handles) != frozen:
                raise batch_contract.ContractError("proposal_context_drift")
        relations = reconciliation.validate_relations(
            raw_relations,
            allowed_handles=handles,
            allowed_tokens=allowed_tokens,
        )
    except batch_contract.ContractError as exc:
        raise AutomationError(exc.code, "本批来源或事项上下文未通过冻结证据检查；未忽略任何错误关联。") from exc
    except reconciliation.ReconciliationError as exc:
        raise AutomationError("proposal_claim_relation", exc.message) from exc
    return {
        "batch_id": batch_id,
        "source_summary": source_summary,
        "candidates": candidates,
        "claim_relations": relations,
        "no_business_change": no_business_change,
    }


def safe_codex_error(stderr: str, returncode: int) -> str:
    lower = stderr.lower()
    if "invalid_json_schema" in lower:
        return "codex_schema_invalid"
    if "usage limit" in lower or "rate limit" in lower or "quota" in lower:
        return "codex_subscription_limit"
    if "not logged in" in lower or "authentication" in lower or "unauthorized" in lower:
        return "codex_subscription_auth"
    if "model" in lower and ("not found" in lower or "unavailable" in lower):
        return "codex_model_unavailable"
    return f"codex_exit_{returncode}"


def isolated_codex_flags() -> list[str]:
    result: list[str] = []
    for feature in CODEX_DISABLED_FEATURES:
        result.extend(["--disable", feature])
    return result


def model_batch_document(config, document):
    # This timestamp describes the wrapper run, not any source event or business fact.
    if (config.get('model_routing') or {}).get('persistent_guard', False):
        return {key:value for key,value in document.items() if key != 'generated_at'}
    return document


def invoke_codex(config: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
    automation = config["dingtalk_automation"]
    # No codex precondition: model_router calls MiMo and Kimi directly, so a
    # missing or hung codex only removes the GPT fallback.
    if not PROMPT_PATH.is_file() or not SCHEMA_PATH.is_file():
        raise AutomationError("codex_contract_missing", "Codex prompt or output schema is missing")
    import model_router
    paths = runtime_paths(config)
    run_dir = paths["runs"] / str(batch["batch_id"])
    ensure_private_dir(run_dir)
    schema_path = run_dir / "output-schema.json"
    batch_contract.save(schema_path, batch_contract.output_schema(json.loads(SCHEMA_PATH.read_text()), batch["document"]))
    prompt = PROMPT_PATH.read_text(encoding="utf-8").rstrip() + "\n\n<batch_data>\n" + canonical_json(model_batch_document(config, batch["document"])) + "\n</batch_data>\n"
    timeout = int(automation.get("codex_timeout_seconds", 900))
    prompt += mimo_schema_prompt(schema_path)
    connection = connect_database(config)
    try:
        connection.execute(
            "UPDATE semantic_batches SET started_at=COALESCE(started_at,?) WHERE batch_id=?",
            (now_iso(), str(batch["batch_id"])),
        )
        connection.commit()
    finally:
        connection.close()
    # Parsing and schema checks run inside the router: an answer rejected here
    # is one provider's failure, so the next model in the chain is asked rather
    # than the whole batch failing (2020-01-02).
    def accept(text: str) -> dict[str, Any]:
        return validate_proposal(config, str(batch["batch_id"]), parse_model_json(text))

    ai_lock = acquire_lock(SHARED_AI_LOCK, blocking=True)
    try:
        try:
            routed = model_router.complete(config, prompt=prompt, purpose="dingtalk_semantic_proposal",
                                           timeout=timeout, workdir=run_dir, validate=accept,
                                           request_context={"scope":"dingtalk-import", "volatile_values":{"batch_id":str(batch["batch_id"])}})
        except model_router.RouterExhausted as exc:
            last = next((a for a in reversed(exc.attempts) if a.get("category") == "invalid_output"), None)
            if last is not None:
                batch_contract.save(run_dir / "model-rejection.json", {
                    "schema_version": 1, "batch_id": batch["batch_id"],
                    "error_code": model_router.legacy_error_code(exc),
                    "rejected_at": now_iso(), "attempts": exc.attempts,
                    "source_count": len(batch["document"]["messages"]),
                    "provided_claim_count": len(batch["document"].get("open_claims", [])),
                })
            raise AutomationError(model_router.legacy_error_code(exc),
                                  f"Model chain exhausted: {exc}") from exc
    finally:
        ai_lock.close()
    reply = routed.text
    proposal = routed.value
    proposal_text = json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write(run_dir / "proposal.json", proposal_text)
    connection = connect_database(config)
    try:
        connection.execute(
            """
            UPDATE semantic_batches SET status='model_complete',started_at=COALESCE(started_at,?),
              proposal_sha256=? WHERE batch_id=?
            """,
            (now_iso(), sha256_text(proposal_text), str(batch["batch_id"])),
        )
        connection.commit()
    finally:
        connection.close()
    return proposal


def semantic_model_called_this_slot(config: dict[str, Any]) -> bool:
    current = datetime.now().astimezone()
    slot_minute = 0 if current.minute < 30 else 30
    start = current.replace(minute=slot_minute, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    connection = connect_database(config)
    try:
        return bool(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_batches WHERE started_at>=? AND started_at<?",
                (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def eligible_queue_count(config: dict[str, Any]) -> int:
    connection = connect_database(config)
    try:
        return int(
            connection.execute(
                """SELECT COUNT(*) FROM semantic_queue q WHERE q.state IN ('pending','failed','deferred')
                   AND NOT EXISTS (SELECT 1 FROM batch_messages b
                     JOIN semantic_recovery_holds h ON h.batch_id=b.batch_id
                     JOIN semantic_queue original ON original.queue_id=b.queue_id
                     WHERE original.message_id=q.message_id AND original.content_sha256=q.content_sha256)"""
            ).fetchone()[0]
        )
    finally:
        connection.close()


def release_batch_failure(config: dict[str, Any], batch_id: str, error_code: str) -> None:
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if error_code in commit_guard.RECOVERABLE:
            connection.execute("INSERT OR IGNORE INTO semantic_recovery_holds VALUES(?,?,?)", (batch_id,error_code,now_iso()))
        connection.execute(
            """
            UPDATE semantic_queue SET state='failed',batch_id=NULL,last_error_code=?,last_attempt_at=?
            WHERE batch_id=? AND state='claimed'
            """,
            (error_code, now_iso(), batch_id),
        )
        connection.execute(
            "UPDATE semantic_batches SET status='failed',completed_at=?,error_code=? WHERE batch_id=?",
            (now_iso(), error_code, batch_id),
        )
        connection.commit()
    finally:
        connection.close()


def refresh_rolled_back_sources(config, connection, batch_id: str) -> bool:
    """Retire only a proved rollback whose frozen source changed; never reuse its proposal."""
    directory = runtime_paths(config)['runs'] / batch_id
    journal_path = directory/'apply_journal.json'
    if not journal_path.is_file(): return False
    lock = acquire_lock(MEMORY_LOCK, blocking=True)
    try:
        connection.execute('BEGIN IMMEDIATE')
        old = connection.execute('SELECT * FROM semantic_batches WHERE batch_id=?',(batch_id,)).fetchone()
        if not old or old['status']!='failed' or connection.execute('SELECT 1 FROM semantic_recovery_holds WHERE batch_id=?',(batch_id,)).fetchone():
            connection.rollback();return False
        journal=json.loads(journal_path.read_text())
        if journal.get('batch_id')!=batch_id or journal.get('state')!='rolled_back' or journal.get('validation_status')!='passed':
            connection.rollback();return False
        manifest=json.loads((directory/'input-manifest.json').read_text())
        if (batch_contract.digest(manifest)!=old['input_manifest_sha256'] or manifest.get('batch_id')!=batch_id
                or batch_contract.digest(manifest.get('document'))!=manifest.get('document_sha256')
                or sha256_path(directory/'proposal.json')!=old['proposal_sha256']):
            raise AutomationError('proposal_recovery_evidence_invalid','旧提案证据变化，保留原队列。')
        rows=[dict(r) for r in connection.execute('SELECT q.*,m.content_sha256 current_sha256 FROM batch_messages b JOIN semantic_queue q USING(queue_id) JOIN messages m USING(message_id) WHERE b.batch_id=? ORDER BY b.ordinal',(batch_id,))]
        if len(rows)!=old['message_count'] or not any(r['content_sha256']!=r['current_sha256'] for r in rows):
            connection.rollback();return False
        if any(r['state']!='failed' or r['batch_id'] is not None for r in rows):
            connection.rollback();return False
        frozen=manifest.get('sources',[])
        if len(frozen)!=len(rows) or any(f.get('queue_id')!=r['queue_id'] or f.get('content_sha256')!=r['content_sha256'] for f,r in zip(frozen,rows)):
            raise AutomationError('proposal_recovery_evidence_invalid','旧来源版本无法核验。')
        changed=[r for r in rows if r['content_sha256']!=r['current_sha256']]
        if any(not connection.execute('SELECT 1 FROM semantic_queue WHERE message_id=? AND content_sha256=?',(r['message_id'],r['current_sha256'])).fetchone() for r in changed):
            connection.rollback();return False
        derived=derived_index_targets(runtime_paths(config)['memory_root'])
        for entry in journal.get('paths',[]):
            target=Path(entry['target'])
            if not safe_target(runtime_paths(config)['memory_root'],target) or target.is_symlink():
                raise AutomationError('rollback_unsafe_target','回退目标不可核验。')
            if target.resolve() not in derived and sha256_path(target)!=entry.get('before_sha256'):
                connection.rollback();return False
        if not journal.get('paths'):
            connection.rollback();return False
        receipt={'schema_version':1,'batch_id':batch_id,'phase':'prepared','at':now_iso(),
          'reason':'current_source_changed_after_verified_rollback','old_batch':dict(old),
          'old_queue':rows,'journal_sha256':sha256_path(journal_path),'manifest_sha256':old['input_manifest_sha256'],
          'proposal_sha256':old['proposal_sha256'],'business_writes':0,'source_links_revalidated_on_next_model_run':True}
        path=directory/'source-refresh.json';batch_contract.save(path,receipt)
        for r in rows:
            if r['content_sha256']!=r['current_sha256']:
                # Existing collector convention: superseded source version, no business judgment.
                connection.execute("UPDATE semantic_queue SET state='no_business_change',applied_at=?,last_error_code='superseded_before_processing' WHERE queue_id=?",(now_iso(),r['queue_id']))
            else:
                connection.execute("UPDATE semantic_queue SET state='pending',last_error_code='source_refresh_required' WHERE queue_id=?",(r['queue_id'],))
        connection.execute("UPDATE semantic_batches SET status='rolled_back' WHERE batch_id=?",(batch_id,))
        connection.commit()
        receipt.update(phase='requeued',refreshed_at=now_iso(),unchanged_sources_requeued=len(rows)-len(changed),superseded_versions=len(changed))
        batch_contract.save(path,receipt)
        return True
    except BaseException:
        connection.rollback();raise
    finally: lock.close()


def retire_unreusable_zero_write_batch(config, connection, batch_id: str) -> bool:
    """Retire a failed proposal that wrote nothing once a member source has moved on.

    The collector supersedes a failed member when its message gets a new version, so
    the frozen proposal can never be reused, while select_batch_rows keeps excluding the
    remaining members and eligible_queue_count keeps counting them: the queue never drains.
    """
    directory = runtime_paths(config)['runs'] / batch_id
    if (directory/'apply_journal.json').exists(): return False  # file transactions belong to rollback recovery
    pre = connection.execute('SELECT status FROM semantic_batches WHERE batch_id=?',(batch_id,)).fetchone()
    if not pre or pre['status']!='failed': return False
    lock = acquire_lock(MEMORY_LOCK, blocking=True)
    try:
        connection.execute('BEGIN IMMEDIATE')
        old = connection.execute('SELECT * FROM semantic_batches WHERE batch_id=?',(batch_id,)).fetchone()
        if (not old or old['status']!='failed' or not old['proposal_sha256'] or (directory/'apply_journal.json').exists()
                or connection.execute('SELECT 1 FROM semantic_recovery_holds WHERE batch_id=?',(batch_id,)).fetchone()):
            connection.rollback();return False
        if old['error_code']=='recovery_conflict_requires_review' and not connection.execute(
                "SELECT 1 FROM recovery_resolutions WHERE old_batch_id=? AND state='verified'",(batch_id,)).fetchone():
            connection.rollback();return False  # unresolved conflicts stay with conflict recovery
        commit_path = directory/'commit-receipt.json'
        commit = json.loads(commit_path.read_text()) if commit_path.is_file() else None
        if commit is not None and (commit.get('batch_id')!=batch_id or commit.get('write_count')!=0
                or (commit.get('files_result') or {}).get('writes',0)!=0 or commit.get('relation_attempt')
                or (commit.get('relation_receipt') or {}).get('status','noop')!='noop'):
            connection.rollback();return False
        if sha256_path(directory/'proposal.json')!=old['proposal_sha256']:
            raise AutomationError('proposal_recovery_evidence_invalid','旧提案证据变化，保留原队列。')
        if commit is not None:
            # Relations are submitted after files_complete without a durable pre-receipt; only
            # a proposal with no relations can prove that nothing was registered.
            if json.loads((directory/'proposal.json').read_text()).get('claim_relations'):
                connection.rollback();return False
            try:
                planned = json.loads((directory/'planned-writes.json').read_text())
            except (OSError, ValueError) as exc:
                raise AutomationError('proposal_recovery_evidence_invalid','零写入计划不可核验，保留原队列。') from exc
            if (planned.get('batch_id')!=batch_id or planned.get('writes')!={} or commit.get('file_hashes',{})!={}
                    or batch_contract.digest(planned)!=commit.get('planned_writes_sha256')):
                raise AutomationError('proposal_recovery_evidence_invalid','零写入计划与提交证据不一致，保留原队列。')
        rows=[dict(r) for r in connection.execute('SELECT q.*,m.content_sha256 current_sha256 FROM batch_messages b JOIN semantic_queue q USING(queue_id) JOIN messages m USING(message_id) WHERE b.batch_id=? ORDER BY b.ordinal',(batch_id,))]
        live=[r for r in rows if r['state']=='failed' and r['batch_id'] is None]
        settled=[r for r in rows if r['state'] in ('applied','no_business_change')]
        if len(rows)!=old['message_count'] or not live or len(live)+len(settled)!=len(rows):
            connection.rollback();return False
        changed=[r for r in live if r['content_sha256']!=r['current_sha256']]
        if not settled and not changed:
            connection.rollback();return False  # still reusable as frozen
        if any(not connection.execute('SELECT 1 FROM semantic_queue WHERE message_id=? AND content_sha256=?',(r['message_id'],r['current_sha256'])).fetchone() for r in changed):
            connection.rollback();return False
        receipt={'schema_version':1,'batch_id':batch_id,'phase':'prepared','at':now_iso(),
          'reason':'unreusable_zero_write_proposal_retired','old_batch':dict(old),'old_queue':rows,
          'commit_phase':(commit or {}).get('phase'),'manifest_sha256':old['input_manifest_sha256'],
          'proposal_sha256':old['proposal_sha256'],'business_writes':0,'source_links_revalidated_on_next_model_run':True}
        path=directory/'source-refresh.json';batch_contract.save(path,receipt)
        for r in live:
            if r['content_sha256']!=r['current_sha256']:
                connection.execute("UPDATE semantic_queue SET state='no_business_change',applied_at=?,last_error_code='superseded_before_processing' WHERE queue_id=?",(now_iso(),r['queue_id']))
            else:
                connection.execute("UPDATE semantic_queue SET state='pending',last_error_code='source_refresh_required' WHERE queue_id=?",(r['queue_id'],))
        connection.execute("UPDATE semantic_batches SET status='rolled_back' WHERE batch_id=?",(batch_id,))
        connection.commit()
        receipt.update(phase='requeued',refreshed_at=now_iso(),unchanged_sources_requeued=len(live)-len(changed),
                       superseded_versions=len(changed),already_settled=len(settled))
        batch_contract.save(path,receipt)
        return True
    except BaseException:
        connection.rollback();raise
    finally: lock.close()


def claim_reusable_proposal(
    config: dict[str, Any], *, recover: bool = True, only_batch: Optional[str] = None,
) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    import dingtalk_prepared_recovery
    if recover:
        prepared = dingtalk_prepared_recovery.prepare_pending(config)
        children = [row['child_batch_id'] for row in prepared.get('batches', {}).values()
                    if row.get('status') == 'child_pending' and row.get('child_batch_id')]
        if children:
            only_batch = children[0]
    paths = runtime_paths(config)
    connection = connect_database(config)
    try:
        batches = connection.execute(
            """
            SELECT batch_id,message_count,text_chars,proposal_sha256
            FROM semantic_batches
            WHERE status IN ('model_complete','failed') AND proposal_sha256 IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM semantic_recovery_holds h WHERE h.batch_id=semantic_batches.batch_id)
            ORDER BY created_at
            """
        ).fetchall()
        for batch_row in batches:
            batch_id = str(batch_row["batch_id"])
            if only_batch is not None and batch_id != only_batch:
                continue
            # Transferred/completed parents are historical, not executable proposals.
            live = connection.execute("SELECT COUNT(*) FROM batch_messages b JOIN semantic_queue q USING(queue_id) WHERE b.batch_id=? AND q.state IN ('failed','claimed') AND (q.batch_id IS NULL OR q.batch_id=?)", (batch_id,batch_id)).fetchone()[0]
            if not live:
                continue
            if refresh_rolled_back_sources(config, connection, batch_id):
                continue
            if retire_unreusable_zero_write_batch(config, connection, batch_id):
                continue
            proposal_path = paths["runs"] / batch_id / "proposal.json"
            try:
                proposal_text = proposal_path.read_text(encoding="utf-8")
                if sha256_text(proposal_text) != str(batch_row["proposal_sha256"]):
                    raise AutomationError("proposal_recovery_evidence_invalid", "冻结提案已变化，消息保留。")
                raw_proposal = json.loads(proposal_text)
                commit_path = paths["runs"] / batch_id / "commit-receipt.json"
                commit = json.loads(commit_path.read_text()) if commit_path.is_file() else {}
                proposal = validate_proposal(config, batch_id, raw_proposal,
                                             check_context=commit.get("phase") not in {"files_complete", "relations_complete", "validation_complete", "complete"})
            except (OSError, json.JSONDecodeError) as exc:
                raise AutomationError("proposal_recovery_evidence_invalid", "冻结提案不可核验，消息保留。") from exc
            except AutomationError as exc:
                # The collector may supersede a member after the retire check released its lock.
                if exc.code == "proposal_source_drift" and retire_unreusable_zero_write_batch(config, connection, batch_id):
                    continue
                raise
            rows = connection.execute(
                """
                SELECT bm.queue_id,bm.ordinal,bm.source_token,q.state,q.batch_id,
                       q.content_sha256,m.content_sha256 AS current_sha256,m.create_time
                FROM batch_messages bm
                JOIN semantic_queue q ON q.queue_id=bm.queue_id
                JOIN messages m ON m.message_id=q.message_id
                WHERE bm.batch_id=? ORDER BY bm.ordinal
                """,
                (batch_id,),
            ).fetchall()
            if len(rows) != int(batch_row["message_count"]):
                continue
            reusable = all(
                str(row["state"]) in ("failed", "claimed")
                and str(row["content_sha256"]) == str(row["current_sha256"])
                and (not row["batch_id"] or str(row["batch_id"]) == batch_id)
                for row in rows
            )
            if not reusable:
                continue
            timestamp = now_iso()
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM semantic_recovery_holds WHERE batch_id=?", (batch_id,)).fetchone():
                connection.rollback()
                continue
            current_rows = connection.execute(
                "SELECT bm.queue_id,bm.ordinal,bm.source_token,q.state,q.batch_id,"
                "q.content_sha256,m.content_sha256 AS current_sha256,m.create_time "
                "FROM batch_messages bm JOIN semantic_queue q ON q.queue_id=bm.queue_id "
                "JOIN messages m ON m.message_id=q.message_id WHERE bm.batch_id=? ORDER BY bm.ordinal", (batch_id,)
            ).fetchall()
            if [tuple(row) for row in current_rows] != [tuple(row) for row in rows]:
                connection.rollback()
                continue
            connection.executemany(
                """
                UPDATE semantic_queue SET state='claimed',batch_id=?,attempt_count=attempt_count+1,
                  last_attempt_at=?,last_error_code=NULL WHERE queue_id=?
                """,
                [(batch_id, timestamp, int(row["queue_id"])) for row in rows],
            )
            connection.execute(
                "UPDATE semantic_batches SET status='model_complete',error_code=NULL,error_detail=NULL WHERE batch_id=?",
                (batch_id,),
            )
            connection.commit()
            manifest = batch_contract.load(paths["runs"] / batch_id, connection, batch_id)
            document = manifest["document"] if manifest else {
                "batch_id": batch_id,
                "messages": [{"source_token": str(row["source_token"]), "time": str(row["create_time"])} for row in rows],
            }
            batch = {
                "batch_id": batch_id,
                "document": document,
                "message_count": len(rows),
                "text_chars": int(batch_row["text_chars"] or 0),
            }
            return batch, proposal
        return None
    finally:
        connection.close()


def apply_proposal_with_recovery(config, batch, proposal):
    """One bounded reconstruction; batch tracks the actual queue owner on failure."""
    try:
        return apply_proposal(config, batch, proposal)
    except AutomationError as exc:
        if exc.code not in commit_guard.RECOVERABLE:
            raise
        old = str(batch['batch_id'])
        directory = runtime_paths(config)['runs'] / old
        commit_guard.record_failure(directory, exc.code, failure_notification_detail(exc), stage='commit_preflight')
        release_batch_failure(config, old, exc.code)
        import dingtalk_prepared_recovery as recovery
        result = recovery.try_prepare(config, old)
        if result is None:
            assessment = json.loads((directory/'recovery-assessment.json').read_text())
            exc.notification_detail['recovery_reason'] = assessment['recovery_reason']
            raise
        # Bind the caller's error handling to the child immediately after ownership transfer.
        child_id = result['child_batch_id']
        batch['batch_id'] = child_id
        reusable = claim_reusable_proposal(config, recover=False, only_batch=child_id)
        if reusable is None:
            raise AutomationError('prepared_recovery_child_unavailable', '恢复批次已保留，等待核验。')
        child, child_proposal = reusable
        batch.clear(); batch.update(child)
        return apply_proposal(config, batch, child_proposal)


def canonical_memory_body(body: str) -> str:
    return "\n\n" + body.lstrip("\n")


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_note(frontmatter: list[tuple[str, Any]], body: str) -> str:
    normalized = canonical_memory_body(body)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    lines = []
    for key, value in frontmatter:
        if key == "content_hash":
            value = f"sha256:{digest}"
        if value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {yaml_string(str(item))}" for item in value)
        elif isinstance(value, str) and key in {
            "source_ref", "content_hash", "observed_at", "processed_at", "reviewed_at", "valid_until"
        }:
            lines.append(f"{key}: {yaml_string(value)}")
        else:
            lines.append(f"{key}: {value}")
    return "---\n" + "\n".join(lines) + "\n---" + normalized


def set_frontmatter_scalar(raw: str, key: str, rendered: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    if pattern.search(raw):
        return pattern.sub(f"{key}: {rendered}", raw, count=1)
    lines = raw.splitlines(keepends=True)
    insertion = next((index for index, line in enumerate(lines) if line.startswith("applies_to:")), len(lines))
    lines.insert(insertion, f"{key}: {rendered}\n")
    return "".join(lines)


def rebuild_existing_note(raw: str, body: str, updates: dict[str, Any]) -> str:
    updated = raw
    for key, value in updates.items():
        rendered = "null" if value is None else yaml_string(str(value))
        updated = set_frontmatter_scalar(updated, key, rendered)
    normalized = canonical_memory_body(body)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    updated = set_frontmatter_scalar(updated, "content_hash", yaml_string(f"sha256:{digest}"))
    return "---\n" + updated.rstrip() + "\n---" + normalized


def append_section_entry(body: str, heading: str, entry: str, marker: str) -> str:
    if marker in body:
        return body
    heading_pattern = re.compile(rf"(^## {re.escape(heading)}[ \t]*\n)(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    match = heading_pattern.search(body)
    if match:
        content = match.group(2).rstrip()
        replacement = match.group(1) + (content + "\n" if content else "") + entry.rstrip() + "\n\n"
        return body[: match.start()] + replacement + body[match.end() :].lstrip("\n")
    new_section = f"## {heading}\n\n{entry.rstrip()}\n\n"
    update_match = re.search(r"^## 更新记录[ \t]*$", body, re.MULTILINE)
    if update_match:
        return body[: update_match.start()].rstrip() + "\n\n" + new_section + body[update_match.start() :]
    return body.rstrip() + "\n\n" + new_section


def safe_filename(title: str, max_length: int = 70) -> str:
    value = re.sub(r"[\x00/\\:*?\"<>|\r\n\[\]#^]+", "-", title).strip(" .-")
    value = re.sub(r"\s+", "", value)
    return (value or "钉钉业务记忆")[:max_length]


def next_memory_id(
    records: list[dict[str, Any]],
    date_key: str,
    reserved: set[str],
    *,
    vault_root: Optional[Path] = None,
) -> str:
    values = []
    for record in records:
        match = MEMORY_ID_RE.match(str(record["memory_id"]))
        if match and match.group(1) == date_key:
            values.append(int(match.group(2)))
    for memory_id in reserved:
        match = MEMORY_ID_RE.match(memory_id)
        if match and match.group(1) == date_key:
            values.append(int(match.group(2)))
    if vault_root is not None:
        for claim in business_router.scan_claims(Path(vault_root)):
            match = MEMORY_ID_RE.match(str(claim.get("claim_id") or ""))
            if match and match.group(1) == date_key:
                values.append(int(match.group(2)))
    number = max(values, default=0) + 1
    if number > 999:
        raise AutomationError("memory_id_exhausted", f"memory ID sequence is exhausted for {date_key}")
    return f"BUSINESS-{date_key}-{number:03d}"


def normalized_observed_at(document: dict[str, Any]) -> str:
    values = [str(message.get("time") or "") for message in document.get("messages", [])]
    latest = max((value for value in values if value), default=now_iso())
    try:
        parsed = chat_sync.parse_timestamp(latest)
        return parsed.isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return now_iso()


def source_summary_change(
    memory_root: Path,
    records: list[dict[str, Any]],
    reserved_ids: set[str],
    batch: dict[str, Any],
    summary: dict[str, Any],
) -> tuple[Path, str, str]:
    observed_at = normalized_observed_at(batch["document"])
    date_key = observed_at[:10].replace("-", "")
    source_ref = f"dingtalk-batch:{batch['batch_id']}"
    timestamp = now_iso()
    candidates = [
        record
        for record in records
        if record["source_system"] == "dingtalk"
        and record["memory_scope"] == "conversation_source"
        and str(record["source_ref"]).startswith("dingtalk-batch:")
        and str(record["observed_at"]).startswith(observed_at[:10])
    ]
    if len(candidates) > 1:
        raise AutomationError("memory_daily_summary_collision", "more than one daily DingTalk source summary exists")
    marker = f"<!-- {source_ref} -->"
    entry = f"{marker}\n- {timestamp}｜{summary['summary']}｜来源：`{source_ref}`\n- 证据边界：{summary['evidence_boundary']}"
    if len(candidates) == 1:
        record = candidates[0]
        body = append_section_entry(str(record["body"]), "自动增量记录", entry, marker)
        text = rebuild_existing_note(
            str(record["raw_frontmatter"]),
            body,
            {"source_ref": source_ref, "processed_at": timestamp},
        )
        return Path(record["path"]), text, str(record["memory_id"])
    memory_id = next_memory_id(
        records,
        date_key,
        reserved_ids,
        vault_root=memory_root.parent,
    )
    reserved_ids.add(memory_id)
    filename = f"{date_key}-钉钉-业务聊天摘要.md"
    path = memory_root / "04_对话来源摘要" / filename
    if path.exists():
        raise AutomationError("memory_daily_summary_collision", "daily DingTalk source summary path already exists")
    body = (
        f"## 对话摘要\n\n{summary['summary']}\n\n"
        f"## 证据边界\n\n{summary['evidence_boundary']}\n\n"
        f"## 来源\n\n- `{source_ref}`；真实消息与会话标识仅保存在本机来源库。\n\n"
        f"## 更新记录\n\n- {timestamp}：由小时自动化建立来源摘要。\n"
    )
    frontmatter = [
        ("memory_id", memory_id),
        ("category", "conversation_summary"),
        ("memory_scope", "conversation_source"),
        ("memory_kind", "conversation_summary"),
        ("origin_type", "internal_communication"),
        ("status", "active"),
        ("evidence_level", "internal_source"),
        ("sensitivity", "confidential"),
        ("source_system", "dingtalk"),
        ("source_check", "matched"),
        ("fact_status", "not_applicable"),
        ("source_ref", source_ref),
        ("observed_at", observed_at),
        ("processed_at", timestamp),
        ("reviewed_at", None),
        ("valid_until", None),
        ("supersedes", []),
        ("content_hash", ""),
        ("applies_to", ["project:business-energy"]),
    ]
    return path, render_note(frontmatter, body), memory_id


def pending_candidate_change(
    config: dict[str, Any],
    records: list[dict[str, Any]],
    reserved_ids: set[str],
    batch: dict[str, Any],
    candidate: dict[str, Any],
    page_overrides: Optional[dict[Path, str]] = None,
) -> tuple[Path, str, str]:
    del records, reserved_ids
    observed_at = normalized_observed_at(batch["document"])
    source_ref = f"dingtalk-batch:{batch['batch_id']}"
    try:
        path, text, route = business_router.route_claim_change(
            config,
            {
                **candidate,
                "evidence_state": "conflicting" if candidate["action"] == "conflict" else "pending",
            },
            source_system="dingtalk",
            source_ref=source_ref,
            observed_at=observed_at,
            page_overrides=page_overrides,
        )
    except business_router.RoutingError as exc:
        quarantine = business_router.quarantine_candidate(
            config,
            {**candidate, "source_system": "dingtalk", "source_ref": source_ref},
            exc.code,
        )
        raise AutomationError(
            "business_routing_failed",
            f"business-page routing failed; candidate quarantined at {quarantine.name}",
        ) from exc
    return path, text, route.claim_id


def linked_candidate_change(
    record: dict[str, Any],
    batch_id: str,
    candidate: dict[str, Any],
) -> tuple[Path, str, str]:
    source_ref = f"dingtalk-batch:{batch_id}"
    marker = f"<!-- {source_ref}:{sha256_text(candidate['dedupe_key'])[:16]} -->"
    entry = f"{marker}\n- {now_iso()}｜{candidate['summary']}｜来源：`{source_ref}`。未改变原条目的事实状态。"
    body = append_section_entry(str(record["body"]), "钉钉来源补充", entry, marker)
    text = rebuild_existing_note(str(record["raw_frontmatter"]), body, {})
    return Path(record["path"]), text, str(record["memory_id"])


def build_memory_changes(
    config: dict[str, Any],
    batch: dict[str, Any],
    proposal: dict[str, Any],
) -> tuple[dict[Path, str], list[str], list[str]]:
    memory_root = runtime_paths(config)["memory_root"]
    records = memory_records(memory_root)
    records_by_id = {str(record["memory_id"]): record for record in records}
    reserved_ids: set[str] = set()
    changes: dict[Path, str] = {}
    target_ids: list[str] = []
    conflicts: list[str] = []
    if proposal["source_summary"] is not None:
        path, text, memory_id = source_summary_change(
            memory_root, records, reserved_ids, batch, proposal["source_summary"]
        )
        changes[path] = text
        target_ids.append(memory_id)
    for candidate in proposal["candidates"]:
        import dingtalk_conflict_recovery
        candidate = dingtalk_conflict_recovery.candidate_override(config, str(batch["batch_id"]), candidate)
        if candidate["action"] == "link_existing":
            record = records_by_id.get(str(candidate["existing_memory_id"]))
            if not record:
                raise AutomationError("proposal_existing_missing", "proposed existing memory ID is not present")
            if record["status"] != "active" or record["fact_status"] != "confirmed":
                candidate = {
                    **candidate,
                    "action": "annotate_page",
                    "evidence_boundary": (
                        f"{candidate['evidence_boundary']} 模型建议关联的旧记忆尚未处于已确认状态，"
                        "本次已安全降级为所属业务页的原位核验说明。"
                    ),
                }
                path, text, memory_id = pending_candidate_change(
                    config, records, reserved_ids, batch, candidate, changes
                )
            else:
                path, text, memory_id = linked_candidate_change(record, str(batch["batch_id"]), candidate)
        else:
            if candidate["action"] == "conflict" and str(candidate["existing_memory_id"]) not in records_by_id:
                raise AutomationError("proposal_existing_missing", "conflict target memory ID is not present")
            path, text, memory_id = pending_candidate_change(
                config, records, reserved_ids, batch, candidate, changes
            )
            if candidate["action"] == "conflict":
                conflicts.append(candidate["title"])
        if path in changes and changes[path] != text and not isinstance(text, business_router.RoutedText):
            raise AutomationError("memory_target_collision", "two proposals target the same note incompatibly")
        changes[path] = text
        target_ids.append(memory_id)
    return changes, list(dict.fromkeys(target_ids)), conflicts


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_local_verification_command(
    config: dict[str, Any],
    command: list[str],
    *,
    phase: str,
    label: str,
    timeout_seconds: int,
    environment: Optional[dict[str, str]] = None,
    attempts: Optional[int] = None,
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Retry only deterministic local verification; never retry model or delivery calls."""
    automation = config["dingtalk_automation"]
    max_attempts = int(
        attempts
        if attempts is not None
        else automation.get("local_verification_attempts", 3)
    )
    max_attempts = max(1, min(max_attempts, 3))
    retry_delay = max(0, min(int(automation.get("local_verification_retry_delay_seconds", 5)), 30))
    paths = runtime_paths(config)
    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            retrying = attempt < max_attempts
            append_log(
                paths["log"],
                "local_verification_timeout",
                phase=phase,
                attempt=attempt,
                max_attempts=max_attempts,
                timeout_seconds=timeout_seconds,
                auto_repair="retrying" if retrying else "retry_exhausted",
            )
            if retrying:
                time.sleep(retry_delay)
                continue
            raise AutomationError(
                f"{phase}_timeout",
                f"{label}连续 {max_attempts} 次超过 {timeout_seconds} 秒执行时限",
                notification_detail={
                    "phase": phase,
                    "reason": f"{label}连续 {max_attempts} 次超过 {timeout_seconds} 秒执行时限。",
                    "auto_repair": "retry_exhausted",
                    "attempts": max_attempts,
                    "timeout_seconds": timeout_seconds,
                },
            ) from exc
        if attempt > 1 and result.returncode == 0:
            append_log(
                paths["log"],
                "local_verification_recovered",
                phase=phase,
                attempt=attempt,
                max_attempts=max_attempts,
                auto_repair="retry_succeeded",
            )
        return result, attempt
    raise AssertionError("local verification retry loop did not return")


def assert_validation_access(config: dict[str, Any]) -> None:
    """Production repair preflight; never try writes after an incomplete scan."""
    live = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.is_file() else {}
    if not config.get("memory_root") or config.get("memory_root") != live.get("memory_root"):
        return
    import validation_evidence
    try:
        validation_evidence.context(ROOT)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        if isinstance(exc, validation_evidence.InputsChanged):
            code, category = "validation_inputs_changed", "inputs_changed"
        elif isinstance(exc, PermissionError):
            code, category = "validation_permission_denied", "permission_denied"
        elif isinstance(exc, sqlite3.Error):
            code, category = "validation_context_unavailable", "database_read_failed"
        else:
            code, category = "validation_context_unavailable", "source_context_unavailable"
        detail = {"phase": "validation_context", "cause_category": category,
                  "cause_fingerprint": sha256_text(type(exc).__name__ + ":" + category)}
        record_validation_failure(config, {"error_code": code, "errors": [code], **detail})
        raise AutomationError(code, "完整校验依据暂时不可用，保留原数据等待核验。", notification_detail=detail) from exc


def record_validation_failure(config, receipt, run_id=None):
    import validation_failure_receipts
    import validation_evidence
    paths = runtime_paths(config)
    live = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.is_file() else {}
    failure_path = (ROOT / "state/last-memory-validation-failure.json" if config.get("memory_root") == live.get("memory_root")
                    else paths["status"].with_name("last-memory-validation-failure.json"))
    receipt = {"recorded_at": now_iso(), "run_id": run_id or os.environ.get("DINGTALK_ACTION_RUN_ID"),
               "origin": "dingtalk_memory", "phase": "validation", "verdict": "fail", **receipt}
    try:
        receipt["validation_context"] = validation_evidence.context(ROOT) if config.get("memory_root") == live.get("memory_root") else None
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        receipt["validation_context"] = None
    validation_failure_receipts.publish(failure_path, receipt)


def run_refresh_and_validate(
    config: dict[str, Any],
    *,
    lock_held: bool = False,
    refresh_indexes: bool = True,
    attempts: Optional[int] = None,
    after_refresh: Optional[Callable[[], None]] = None,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    paths = runtime_paths(config)
    automation = config["dingtalk_automation"]
    assert_validation_access(config)
    environment = dict(os.environ)
    if lock_held:
        environment["BUSINESS_MEMORY_INDEX_LOCK_HELD"] = "1"
    else:
        environment.pop("BUSINESS_MEMORY_INDEX_LOCK_HELD", None)
    if run_id:
        environment["BUSINESS_VALIDATION_RUN_ID"] = run_id
    refresh_exit: Optional[int] = None
    refresh_attempts = 0
    if refresh_indexes and (config.get("source_governance") or {}).get("inventory_auto_refresh") is True:
        # Register Example Owner's own file renames/edits before judging this write.
        # The automatic validator does the same, but only once the vault has
        # been quiet for a while; a write landing in between failed on the stale
        # inventory and was rolled back (2020-01-02: three times in one day).
        inventory, _ = run_local_verification_command(
            config,
            ["/usr/bin/ruby", str(ROOT / "bin" / "refresh_internal_memory_ledger.rb"), "--inventory-only",
             str(Path(paths["memory_root"]).parent), str(paths["external_root"])],
            phase="source_inventory_refresh",
            label="来源清单刷新",
            timeout_seconds=int(automation.get("memory_index_refresh_timeout_seconds", 120)),
            environment=environment,
            attempts=attempts,
        )
        if inventory.returncode != 0:
            raise AutomationError("source_inventory_refresh_failed", "source inventory refresh failed")
    if refresh_indexes:
        try:
            refresh, refresh_attempts = run_local_verification_command(
                config,
                ["/usr/bin/ruby", str(REFRESH_SCRIPT)],
                phase="memory_index_refresh",
                label="业务记忆索引刷新",
                timeout_seconds=int(automation.get("memory_index_refresh_timeout_seconds", 120)),
                environment=environment,
                attempts=attempts,
            )
        finally:
            if after_refresh is not None:
                after_refresh()
        refresh_exit = int(refresh.returncode)
        if refresh.returncode != 0:
            raise AutomationError("refresh_failed", "memory index refresh failed")
    try:
        validate, validation_attempts = run_local_verification_command(
            config,
            ["/usr/bin/ruby", str(VALIDATE_SCRIPT), str(paths["memory_root"]), str(paths["external_root"])],
            phase="memory_validation",
            label="业务记忆一致性验收",
            timeout_seconds=int(automation.get("memory_validation_timeout_seconds", 120)),
            environment=environment,
            attempts=attempts,
        )
    except AutomationError as exc:
        record_validation_failure(config, {"error_code": exc.code, "errors": [exc.code]}, run_id)
        raise
    try:
        payload = json.loads(validate.stdout)
    except json.JSONDecodeError as exc:
        record_validation_failure(config, {"error_code": "validation_output", "errors": ["invalid validator JSON"]}, run_id)
        raise AutomationError("validation_output", "memory validator did not return JSON") from exc
    if payload.get("verdict") == "deferred" and payload.get("error_code") == "validation_inputs_changed":
        raise AutomationError("validation_inputs_changed", "资料在校验期间变化，等待新快照后再验；未判为数据损坏")
    if validate.returncode != 0:
        receipt = {
            "recorded_at": now_iso(),
            "run_id": run_id or os.environ.get("DINGTALK_ACTION_RUN_ID"),
            "origin": "dingtalk_memory", "phase": "validation",
            "error_code": payload.get("error_code") or "validation_failed",
            "returncode": validate.returncode,
            "verdict": payload.get("verdict"),
            "errors": payload.get("errors", [])[:50] if isinstance(payload.get("errors"), list) else [],
        }
        record_validation_failure(config, receipt, run_id)
        code = payload.get("error_code")
        if code not in {"validation_permission_denied", "validation_context_unavailable"}:
            code = "validation_failed"
        raise AutomationError(code, "memory validator returned a failure")
    if payload.get("verdict") != "pass" or payload.get("errors"):
        record_validation_failure(config, {"error_code": "validation_failed", "errors": payload.get("errors") or ["validator did not pass"]}, run_id)
        raise AutomationError("validation_failed", "memory validator did not pass")
    return {
        "refresh_exit": refresh_exit,
        "refresh_attempts": refresh_attempts,
        "indexes_refreshed": bool(refresh_indexes),
        "validation_attempts": validation_attempts,
        "validator": payload,
    }


def safe_target(memory_root: Path, path: Path) -> bool:
    memory_root = memory_root.resolve()
    path = path.resolve()
    try:
        relative = path.relative_to(memory_root)
    except ValueError:
        vault_root = memory_root.parent
        try:
            relative = path.relative_to(vault_root)
        except ValueError:
            return False
        return (bool(relative.parts) and path.suffix == ".md"
                and relative.parts[0] in business_router.page_integrity.business_roots(vault_root))
    if relative.parts == ("00_记忆总览.md",):
        return True
    if relative.parts == ("90_待确认", "00_待确认总表.md"):
        return True
    return bool(relative.parts) and relative.parts[0] in {
        "01_关键业务认知",
        "02_内部业务资料",
        "03_外部沟通",
        "04_对话来源摘要",
        "91_归档",
        "99_规则与模板",
    } and path.suffix == ".md"


def write_apply_journal(path: Path, payload: dict[str, Any]) -> None:
    batch_contract.save(path, payload)


def snapshot_applied_targets(path: Path, payload: dict[str, Any]) -> None:
    for entry in payload.get("paths", []):
        # A later refresh must never bless an unrelated edit as our own write.
        entry["applied_sha256"] = entry.get(
            "expected_applied_sha256", sha256_path(Path(str(entry["target"])))
        )
    payload["applied_snapshot_at"] = now_iso()
    write_apply_journal(path, payload)


def derived_index_targets(memory_root: Path) -> set[Path]:
    return {memory_root.resolve() / relative for relative in (
        "00_记忆总览.md", "99_规则与模板/AI检索总表.md", "90_待确认/00_待确认总表.md",
    )}


def batch_source_refs(config: dict[str, Any], batch_id: str) -> Optional[list[str]]:
    """Hash original source identities, without exposing message bodies or IDs."""
    database = runtime_paths(config)["database"]
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        try:
            rows = connection.execute(
                "SELECT q.message_id FROM batch_messages b JOIN semantic_queue q "
                "ON q.queue_id=b.queue_id WHERE b.batch_id=?", (batch_id,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return sorted({sha256_text("dingtalk_chat:" + str(row[0])) for row in rows}) or None


def recovery_conflicts(config: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts = []
    for journal in sorted(runtime_paths(config)["runs"].glob("*/apply_journal.json")):
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AutomationError("rollback_journal_invalid", "recovery journal cannot be verified") from exc
        if payload.get("state") == "recovery_conflict":
            import dingtalk_conflict_recovery
            if not dingtalk_conflict_recovery.completion_verified(config, str(payload.get("batch_id"))):
                conflicts.append(payload)
    return conflicts


def mark_conflict_batch(config: dict[str, Any], payload: dict[str, Any]) -> None:
    """Retain the queue, and exclude overlapping source identities before AI work."""
    if not runtime_paths(config)["database"].is_file():
        return
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status FROM semantic_batches WHERE batch_id=?", (payload.get("batch_id"),)).fetchone()
        if row and row["status"] != "applied":
            connection.execute(
                "UPDATE semantic_batches SET status='failed',error_code='recovery_conflict_requires_review',"
                "error_detail='later business edits preserved; related source messages await review',completed_at=COALESCE(completed_at,?) WHERE batch_id=?",
                (now_iso(), payload["batch_id"]),
            )
            connection.execute(
                "UPDATE semantic_queue SET state='deferred',last_error_code='recovery_conflict_requires_review' "
                "WHERE queue_id IN (SELECT queue_id FROM batch_messages WHERE batch_id=?) AND state IN ('pending','claimed','failed','deferred')",
                (payload["batch_id"],),
            )
        connection.commit()
    finally:
        connection.close()


def ensure_recovery_isolation(
    config: dict[str, Any], batch_id: str, targets: Any,
) -> None:
    """Only continue when BOTH the write set and original sources are disjoint."""
    conflicts = recovery_conflicts(config)
    if not conflicts:
        return
    indexes = derived_index_targets(runtime_paths(config)["memory_root"])
    proposed = {Path(path).resolve() for path in targets} - indexes
    sources = batch_source_refs(config, batch_id)
    for conflict in conflicts:
        import dingtalk_conflict_recovery
        if dingtalk_conflict_recovery.permits_child(config, str(conflict["batch_id"]), batch_id, proposed):
            continue
        affected = {Path(str(entry["target"])).resolve() for entry in conflict.get("paths", [])} - indexes
        affected |= dingtalk_conflict_recovery.mapped_targets(config, str(conflict["batch_id"]))
        previous_sources = conflict.get("source_refs")
        if (batch_id == conflict.get("batch_id") or not sources or not previous_sources
                or proposed & affected or set(sources) & set(previous_sources)):
            raise AutomationError(
                "recovery_conflict_requires_review",
                "既有回退存在后续内容修改，无法证明本批来源与写入目标均独立；原文件和队列已保留。",
                notification_detail={"phase": "rollback_verification", "auto_repair": "conflict_preserved"},
            )


def restore_from_journal(
    config: dict[str, Any],
    journal_path: Path,
    verify: bool = True,
    *,
    lock_held: bool = False,
    verification_attempts: Optional[int] = None,
) -> None:
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal_path.with_name("preserve-current.json").is_file():
        import dingtalk_conflict_recovery
        dingtalk_conflict_recovery.load_approval(config, str(payload["batch_id"]))
        raise AutomationError("recovery_preserve_current_active", "原批次已有保留现版的恢复决定，不再回退或改写旧日志。")
    memory_root = runtime_paths(config)["memory_root"]
    indexes = derived_index_targets(memory_root)
    if payload.get("state") == "rolled_back":
        return
    prepared: list[tuple[dict[str, Any], Path, Optional[Path]]] = []
    conflicts = []
    for entry in payload.get("paths", []):
        target = Path(str(entry["target"]))
        if not safe_target(memory_root, target):
            raise AutomationError("rollback_unsafe_target", "rollback target is outside the allowlist")
        backup = Path(str(entry["backup"])) if entry.get("backup") else None
        if target.exists() and (not target.is_file() or target.is_symlink()):
            raise AutomationError("rollback_unsafe_target", "rollback target is not a regular file")
        if entry.get("existed"):
            if (not backup or not backup.is_file() or backup.is_symlink()
                    or not backup.resolve().is_relative_to((journal_path.parent / "backup").resolve())):
                raise AutomationError("rollback_backup_missing", "rollback backup is missing")
            before_sha256 = entry.get("before_sha256")
            if before_sha256 and sha256_path(backup) != before_sha256:
                raise AutomationError("rollback_backup_mismatch", "rollback backup checksum does not match the journal")
        before = sha256_path(backup) if entry.get("existed") and backup else None
        current = sha256_path(target)
        if target.resolve() in indexes:
            entry["recovery_status"] = "derived_rebuild_pending"
            continue
        if current == before:
            entry["recovery_status"] = "already_restored"
            continue
        expected = entry.get("expected_applied_sha256", entry.get("applied_sha256"))
        if not expected or current != expected:
            entry["recovery_status"] = "conflict_preserved"
            entry["conflict_sha256"] = current
            conflicts.append(entry)
            continue
        prepared.append((entry, target, backup))
    if conflicts:
        # Persist the conflict before returning; never partially restore a batch.
        payload.update(state="recovery_conflict", recovery_checked_at=now_iso(),
                       source_refs=payload.get("source_refs") or batch_source_refs(config, str(payload.get("batch_id") or "")),
                       conflict_count=len(conflicts), validation_status="not_attempted")
        write_apply_journal(journal_path, payload)
        mark_conflict_batch(config, payload)
        raise AutomationError("rollback_concurrent_change", "rollback target changed after the transaction snapshot")
    for entry, target, backup in prepared:
        if entry.get("existed"):
            assert backup is not None
            atomic_write(target, backup.read_text(encoding="utf-8"), mode=int(entry.get("mode") or 0o644))
        elif target.exists():
            target.unlink()
            fsync_directory(target.parent)
        entry["recovery_status"] = "restored"
        entry["restored_at"] = now_iso()
        write_apply_journal(journal_path, payload)
    # This is a durable checkpoint, not a validation/recovery success claim.
    payload.update(state="rollback_pending_validation", files_restored_at=now_iso(), validation_status="pending")
    write_apply_journal(journal_path, payload)
    if verify:
        run_refresh_and_validate(
            config,
            lock_held=lock_held,
            attempts=verification_attempts,
        )
    payload["validation_status"] = "passed" if verify else "not_required_test_verifier"
    payload["state"] = "rolled_back"
    payload["rolled_back_at"] = now_iso()
    write_apply_journal(journal_path, payload)


def recover_apply_journals(config: dict[str, Any], *, lock_held: bool = False) -> int:
    paths = runtime_paths(config)
    if not paths["runs"].is_dir():
        return 0
    recovered = 0
    for journal in paths["runs"].glob("*/apply_journal.json"):
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AutomationError("rollback_journal_invalid", "recovery journal cannot be verified") from exc
        if payload.get("state") == "recovery_conflict":
            import dingtalk_conflict_recovery
            if not dingtalk_conflict_recovery.load_approval(config, str(payload["batch_id"])):
                mark_conflict_batch(config, payload)
        if payload.get("state") not in ("prepared", "applying", "rollback_pending_validation"):
            continue
        lock = None if lock_held else acquire_lock(MEMORY_LOCK, blocking=True)
        try:
            try:
                restore_from_journal(config, journal, lock_held=True)
                recovered += 1
            except AutomationError as exc:
                if exc.code != "rollback_concurrent_change":
                    raise
        finally:
            if lock is not None:
                lock.close()
    conflicts = recovery_conflicts(config)
    atomic_write(paths["status"].with_name("journal_recovery_status.json"), canonical_json({
        "schema_version": 1, "checked_at": now_iso(), "recovered_count": recovered,
        "conflict_count": len(conflicts), "status": "review_required" if conflicts else "clear",
    }) + "\n")
    return recovered


def backup_memory_file(source: Path, destination: Path) -> None:
    last_error: Optional[OSError] = None
    for attempt in range(3):
        try:
            with source.open("rb") as source_handle, destination.open("wb") as destination_handle:
                shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
            destination.chmod(0o600)
            return
        except OSError as exc:
            last_error = exc
            if attempt >= 2:
                break
            time.sleep(attempt + 1)
    raise AutomationError("memory_backup_failed", "memory backup could not be completed") from last_error


def apply_changes(
    config: dict[str, Any],
    batch_id: str,
    changes: dict[Path, str],
    verifier: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    *,
    lock_held: bool = False,
) -> dict[str, Any]:
    ensure_recovery_isolation(config, batch_id, changes)
    if not changes:
        return {"writes": 0, "validated": False}
    if not lock_held:
        lock = acquire_lock(MEMORY_LOCK, blocking=True)
        try:
            return apply_changes(
                config,
                batch_id,
                changes,
                verifier=verifier,
                lock_held=True,
            )
        finally:
            lock.close()
    paths = runtime_paths(config)
    memory_root = paths["memory_root"].resolve()
    for path in changes:
        if not safe_target(memory_root, path):
            raise AutomationError("unsafe_memory_target", "proposed memory path is outside the allowlist")
    run_dir = paths["runs"] / batch_id
    backup_root = run_dir / "backup"
    ensure_private_dir(run_dir)
    ensure_private_dir(backup_root)
    indexes = [
        memory_root / "00_记忆总览.md",
        memory_root / "99_规则与模板" / "AI检索总表.md",
        memory_root / "90_待确认" / "00_待确认总表.md",
    ]
    tracked = list(dict.fromkeys(list(changes.keys()) + indexes))
    journal_entries = []
    for target in tracked:
        target = target.resolve()
        if target.exists() and (not target.is_file() or target.is_symlink()):
            raise AutomationError("unsafe_memory_target", "memory target is not a regular file")
        try:
            relative = target.relative_to(memory_root)
            backup = backup_root / "memory" / relative
        except ValueError:
            relative = target.relative_to(memory_root.parent)
            backup = backup_root / "business" / relative
        if target.is_file():
            ensure_private_dir(backup.parent)
            backup_memory_file(target, backup)
            if sha256_path(target) != sha256_path(backup):
                raise AutomationError("memory_backup_concurrent_change", "target changed while creating its backup")
            journal_entries.append(
                {
                    "target": str(target),
                    "backup": str(backup),
                    "existed": True,
                    "mode": target.stat().st_mode & 0o777,
                    "before_sha256": sha256_path(target),
                }
            )
        else:
            journal_entries.append(
                {
                    "target": str(target),
                    "backup": None,
                    "existed": False,
                    "mode": 0o644,
                    "before_sha256": None,
                }
            )
    journal_path = run_dir / "apply_journal.json"
    expected_changes = {Path(path).resolve(): sha256_text(text) for path, text in changes.items()}
    for entry in journal_entries:
        if Path(entry["target"]) in expected_changes:
            entry["expected_applied_sha256"] = expected_changes[Path(entry["target"])]
    journal_payload = {"schema_version": 2, "batch_id": batch_id, "state": "prepared",
                       "created_at": now_iso(), "paths": journal_entries,
                       "source_refs": batch_source_refs(config, batch_id)}
    write_apply_journal(journal_path, journal_payload)
    try:
        journal_payload["state"] = "applying"
        write_apply_journal(journal_path, journal_payload)
        try:
            for target, text in changes.items():
                if isinstance(text, business_router.RoutedText):
                    try:
                        business_router.verify_expected_hash(target, text)
                    except business_router.RoutingError as exc:
                        raise AutomationError(exc.code, exc.message) from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, text, mode=(target.stat().st_mode & 0o777) if target.exists() else 0o644)
                fsync_directory(target.parent)
        except OSError as exc:
            raise AutomationError("memory_write_failed", "memory note write could not be completed") from exc
        snapshot_applied_targets(journal_path, journal_payload)
        verification = (
            verifier(config)
            if verifier
            else run_refresh_and_validate(
                config,
                lock_held=True,
                after_refresh=lambda: snapshot_applied_targets(journal_path, journal_payload),
            )
        )
        journal_payload["state"] = "committed"
        journal_payload["committed_at"] = now_iso()
        write_apply_journal(journal_path, journal_payload)
        return {"writes": len(changes), "validated": True, "verification": verification}
    except Exception as original_exc:
        original_code = getattr(original_exc, "code", type(original_exc).__name__)
        original_detail = failure_notification_detail(original_exc)
        if not commit_guard.record_failure(run_dir, original_code, original_detail, stage="apply_failure"):
            original_detail["evidence_gap"] = "failure_receipt_unavailable"
        try:
            restore_from_journal(
                config,
                journal_path,
                verify=verifier is None,
                lock_held=True,
                verification_attempts=1,
            )
        except Exception as rollback_exc:
            original_code = (
                original_exc.code
                if isinstance(original_exc, AutomationError)
                else type(original_exc).__name__
            )
            rollback_code = (
                rollback_exc.code
                if isinstance(rollback_exc, AutomationError)
                else type(rollback_exc).__name__
            )
            rollback_detail = failure_notification_detail(
                rollback_exc,
                default_phase="rollback_verification",
            )
            rollback_detail.update(
                {
                    "phase": "rollback_verification",
                    "reason": (
                        f"最初失败为 {original_code}；文件已尝试回滚，但回滚验收又发生 {rollback_code}。"
                    ),
                    "auto_repair": "rollback_unverified",
                    "original_error_code": original_code,
                    "rollback_error_code": rollback_code,
                }
            )
            commit_guard.record_failure(run_dir, original_code, rollback_detail, stage="rollback_failure")
            raise AutomationError(
                f"rollback_{rollback_code}",
                f"rollback verification failed after {original_code}: {rollback_code}",
                notification_detail=rollback_detail,
            ) from rollback_exc
        commit_guard.record_failure(run_dir, original_code, {**original_detail, "rollback_status": "passed"}, stage="rollback_complete")
        raise


def assert_batch_execution(config: dict[str, Any], batch_id: str, *, completed: bool = False) -> None:
    connection = connect_database(config)
    try:
        if connection.execute("SELECT 1 FROM semantic_recovery_holds WHERE batch_id=?", (batch_id,)).fetchone():
            raise AutomationError("prepared_recovery_original_held", "原冲突批次仅保留审计证据，不能再次提交。")
        batch = connection.execute("SELECT message_count FROM semantic_batches WHERE batch_id=?", (batch_id,)).fetchone()
        rows = connection.execute("SELECT q.state,q.batch_id FROM batch_messages b JOIN semantic_queue q ON q.queue_id=b.queue_id WHERE b.batch_id=?", (batch_id,)).fetchall()
        states = {'applied', 'no_business_change'} if completed else {'claimed'}
        if not batch or len(rows) != batch['message_count'] or any(row['batch_id'] != batch_id or row['state'] not in states for row in rows):
            raise AutomationError("proposal_queue_ownership_changed", "消息执行归属已变化，旧执行者不能写入。")
    finally:
        connection.close()


def apply_proposal(config: dict[str, Any], batch: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    batch_id = str(batch["batch_id"])
    run_dir = runtime_paths(config)["runs"] / batch_id
    checkpoint = run_dir / "commit-receipt.json"
    proposal_hash = batch_contract.digest(proposal)
    commit = json.loads(checkpoint.read_text()) if checkpoint.is_file() else None
    if commit and (commit.get("batch_id") != batch_id or commit.get("proposal_sha256") != proposal_hash):
        raise AutomationError("proposal_commit_conflict", "原提交已绑定另一份提案，不能覆盖重做。")
    if commit and commit.get("phase") == "complete":
        assert_batch_execution(config, batch_id, completed=True)
        return commit["result"]
    lock = acquire_lock(MEMORY_LOCK, blocking=True)
    try:
        assert_batch_execution(config, batch_id)
        connection = connect_database(config)
        try:
            batch_contract.load(run_dir, connection, batch_id)
        finally:
            connection.close()

        if commit is None:
            origin_path = run_dir / 'prepared-recovery-origin.json'
            origin = json.loads(origin_path.read_text()) if origin_path.is_file() else None
            if origin and any(sha256_path(Path(p)) != h for p,h in origin['target_hashes'].items()):
                raise AutomationError('proposal_commit_content_conflict', '恢复准备后目标再次变化，保留现版。')
            changes, target_ids, conflicts = build_memory_changes(config, batch, proposal)
            if origin and set(map(str, changes)) != set(origin['target_hashes']):
                raise AutomationError('prepared_recovery_write_set_changed', '恢复提案的目标范围发生变化，未写入。')
            ensure_recovery_isolation(config, batch_id, changes)
            planned = {"schema_version": 1, "batch_id": batch_id,
                       "writes": {str(path): str(text) for path, text in changes.items()}}
            batch_contract.save(run_dir / "planned-writes.json", planned)
            commit = {"schema_version": 1, "batch_id": batch_id, "proposal_sha256": proposal_hash,
                      "phase": "prepared", "prepared_at": now_iso(), "target_memory_ids": target_ids,
                      "conflicts": conflicts, "write_count": len(changes),
                      "file_hashes": {str(path): sha256_text(text) for path, text in changes.items()},
                      "before_hashes": {str(path): sha256_path(Path(path)) for path in changes},
                      "planned_writes_sha256": batch_contract.digest(planned)}
            commit_guard.capture(run_dir, commit)
            batch_contract.save(checkpoint, commit)
        else:
            changes = None
        if commit["phase"] == "prepared":
            journal_path = run_dir / "apply_journal.json"
            journal = json.loads(journal_path.read_text()) if journal_path.is_file() else {}
            if journal.get("state") == "committed":
                if any(sha256_path(Path(path)) != value for path, value in commit["file_hashes"].items()):
                    raise AutomationError("proposal_commit_content_conflict", "已完成的文件后来发生变化，保留现版，不重复覆盖。")
                result = {"writes": commit["write_count"], "validated": True, "resumed_files": True}
            else:
                if journal and journal.get("state") != "rolled_back":
                    raise AutomationError("proposal_commit_recovery_pending", "原文件事务需先安全恢复；没有重复写入。")
                if changes is None:
                    planned = json.loads((run_dir / "planned-writes.json").read_text())
                    if batch_contract.digest(planned) != commit.get("planned_writes_sha256"):
                        raise AutomationError("proposal_commit_plan_corrupt", "原提交快照无法校验，保留文件不重写。")
                    if any(sha256_path(Path(path)) != value for path, value in commit["before_hashes"].items()):
                        raise AutomationError("proposal_commit_content_conflict", "准备期间目标内容发生变化，保留现版。")
                    changes = {Path(path): text for path, text in planned["writes"].items()}
                    if {str(path): sha256_text(text) for path, text in changes.items()} != commit["file_hashes"]:
                        raise AutomationError("proposal_commit_content_conflict", "准备期间目标内容发生变化，保留现版。")
                commit_guard.check(config, run_dir, commit, {str(p): str(t) for p,t in changes.items()}, journal)
                commit_guard.mark_started(run_dir)
                result = apply_changes(config, batch_id, changes, lock_held=True)
            commit.update(phase="files_complete", files_completed_at=now_iso(), files_result=result,
                          relation_context=reconciliation.claim_context_hashes({str(item["claim_handle"]) for item in proposal.get("claim_relations", [])}))
            batch_contract.save(checkpoint, commit)
        elif any(sha256_path(Path(path)) != value for path, value in commit["file_hashes"].items()):
            raise AutomationError("proposal_commit_content_conflict", "已完成的文件后来发生变化，保留现版，不重复覆盖。")
    finally:
        lock.close()
    source_material = {
        str(item["source_token"]): {
            "material": str(item.get("text") or ""),
            "directness": "direct",
            "verification_method": "model_proposed",
        }
        for item in batch.get("document", {}).get("messages", [])
        if item.get("source_token")
    }
    if commit["phase"] == "files_complete":
        expected_context = commit.get("relation_context", {})
        relation_receipt = reconciliation.submit_relations(
            config, list(proposal.get("claim_relations") or []), source_system="dingtalk_chat",
            source_material=source_material, request_id=batch_id,
            observed_at=max((str(item.get("time") or "") for item in batch.get("document", {}).get("messages", [])), default=commit["prepared_at"]),
            expected_context=expected_context,
        )
        if relation_receipt.get("status") not in {"applied", "noop"}:
            commit["relation_attempt"] = relation_receipt
            batch_contract.save(checkpoint, commit)
            raise AutomationError("proposal_relation_registration_pending", "文件阶段已完成，但证据关联未完成；消息仍待处理。")
        commit.update(phase="relations_complete", relations_completed_at=now_iso(), relation_receipt=relation_receipt,
                      file_hashes={path: sha256_path(Path(path)) for path in commit["file_hashes"]})
        batch_contract.save(checkpoint, commit)
    # A validation interrupted just before the queue commit may run again, but
    # files and relation transactions never run a second time.
    validation_lock = acquire_lock(MEMORY_LOCK, blocking=True)
    try:
        verification = run_refresh_and_validate(config, lock_held=True, refresh_indexes=False)
        commit.update(phase="validation_complete", validated_at=now_iso(), verification=verification)
        batch_contract.save(checkpoint, commit)
        completed_at = now_iso()
        connection = connect_database(config)
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch_contract.load(run_dir, connection, batch_id)
            queue_state = "no_business_change" if proposal["no_business_change"] else "applied"
            connection.execute("UPDATE semantic_queue SET state=?,applied_at=?,last_error_code=NULL WHERE batch_id=? AND state='claimed'",
                               (queue_state, completed_at, batch_id))
            remaining = connection.execute("SELECT COUNT(*) FROM batch_messages b JOIN semantic_queue q ON q.queue_id=b.queue_id WHERE b.batch_id=? AND (COALESCE(q.batch_id,'')!=? OR q.state!=?)",
                                           (batch_id, batch_id, queue_state)).fetchone()[0]
            if remaining:
                raise AutomationError("proposal_queue_changed", "待处理消息已变化，没有标记整批完成。")
            connection.execute("UPDATE semantic_batches SET status='applied',completed_at=?,error_code=NULL,error_detail=NULL WHERE batch_id=?", (completed_at, batch_id))
            connection.commit()
        finally:
            connection.close()
        result = {**commit["files_result"], "validated": True, "verification": verification,
                  "target_memory_ids": commit["target_memory_ids"], "conflicts": commit["conflicts"],
                  "claim_relations": commit["relation_receipt"]}
        commit.update(phase="complete", completed_at=completed_at, result=result)
        batch_contract.save(checkpoint, commit)
        return result
    finally:
        validation_lock.close()


def register_pending_decisions(config_path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "/usr/bin/python3",
                str(ROOT / "bin" / "dingtalk_decision_center.py"),
                "--sync-only",
                "--config",
                str(config_path),
            ],
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
            env={**os.environ, "LC_ALL": "en_US.UTF-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise AutomationError(
            "decision_registration_timeout",
            "decision queue registration exceeded 180 seconds",
            notification_detail={
                "phase": "decision_registration",
                "reason": "待确认事项登记超过 180 秒执行时限。",
                "auto_repair": "queue_preserved",
                "attempts": 1,
                "timeout_seconds": 180,
            },
        ) from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AutomationError("decision_registration_invalid", "decision registration did not return JSON") from exc
    if result.returncode != 0 or payload.get("status") != "success":
        raise AutomationError("decision_registration_failed", "decision queue registration failed")
    return payload


def incremental_reconcile_complete(incremental: Any) -> bool:
    """Fail closed: absence, skipped reads and unfinished windows are not coverage."""
    if not isinstance(incremental, dict) or incremental.get("status") != "success":
        return False
    if incremental.get("read_attempted") is not True or incremental.get("read_recovery") or incremental.get("error_code"):
        return False
    window = incremental.get("reconciliation")
    if not isinstance(window, dict) or window.get("status") != "complete" or window.get("list_complete") is not True:
        return False
    if not all(isinstance(window.get(key), str) and window[key] for key in ("window_id", "window_start", "window_end")):
        return False
    if not isinstance(incremental.get("run_id"), str) or not incremental["run_id"] or window.get("last_run_id") != incremental["run_id"]:
        return False
    try:
        start = datetime.fromisoformat(window["window_start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window["window_end"].replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or start.utcoffset() is None or end.utcoffset() is None or start >= end:
            return False
    except (ValueError, TypeError):
        return False
    for row in (incremental, window):
        counts = [row.get(key) for key in ("conversations_discovered", "conversations_complete", "conversations_incomplete")]
        if any(type(value) is not int or value < 0 for value in counts):
            return False
        if counts[2] != 0 or counts[0] != counts[1]:
            return False
    return (all(incremental[key] == window[key] for key in
                ("conversations_discovered", "conversations_complete", "conversations_incomplete"))
            and not window.get("error_code") and not window.get("next_due"))


def collector_status(config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("external_root"):
        return {}
    try:
        value = json.loads((Path(config["external_root"]) / "dingtalk-chat/state/incremental_status.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def chat_source_read_gate(config: dict[str, Any]) -> dict[str, Any]:
    """Observe the collector's hold/due time without spending its attempt budget."""
    import chat_reconcile_checkpoint as checkpoint
    window = checkpoint.read_progress(config)
    previous = collector_status(config)
    evidence = previous.get("read_recovery") or {}
    if window:
        if window.get("status") == "unavailable":
            return {"status": "partial", "read_attempted": False, "run_id": None,
                    "error_code": "chat_checkpoint_busy", "reconciliation": window,
                    "read_recovery": {"error_code": "chat_checkpoint_busy", "checkpoint_status": "unavailable",
                                      "next_retry_at": None},
                    "messages_inserted": 0, "messages_updated": 0, "messages_hidden": 0}
        if window.get("status") == "complete":
            return {}
        if window.get("status") == "manual_required":
            # Invalid checkpoint receipts may intentionally have no attempt counter.
            evidence = (checkpoint.recovery_receipt(window) if "attempt_count" in window else
                        {"error_code": window.get("error_code") or "chat_read_blocked",
                         "next_retry_at": None, "checkpoint_status": "manual_required"})
        elif window.get("next_due"):
            try:
                if datetime.fromisoformat(window["next_due"].replace("Z", "+00:00")) <= datetime.now().astimezone():
                    return {}
            except (ValueError, TypeError):
                evidence = {"error_code": "chat_checkpoint_invalid", "checkpoint_status": "manual_required"}
            else:
                evidence = checkpoint.recovery_receipt(window, waiting=True)
        else:
            return {}
    else:
        blocked = (previous.get("status") == "manual_required"
                   or evidence.get("checkpoint_status") == "manual_required"
                   or evidence.get("error_code") in {"chat_read_auth_required", "chat_read_blocked"})
        if not blocked:
            return {}
    progress = window or previous.get("reconciliation") or {}
    return {"status": "partial", "read_recovery": evidence, "read_attempted": False,
            "run_id": progress.get("last_run_id") or previous.get("run_id"),
            "error_code": evidence.get("error_code") or "chat_read_blocked",
            "messages_inserted": 0, "messages_updated": 0, "messages_hidden": 0,
            **{key: progress.get(key) for key in ("conversations_discovered", "conversations_complete", "conversations_incomplete")},
            "reconciliation": progress}


def collector_payload(stdout: str | bytes | None) -> dict[str, Any]:
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    lines = (stdout or "").splitlines(keepends=True)
    for i in [0] + [i for i, line in enumerate(lines) if i and line.startswith("{")][::-1]:
        try:
            value = json.loads("".join(lines[i:]))
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    return {}


def collector_failure_detail(config: dict[str, Any], payload: dict[str, Any],
                             previous: dict[str, Any], started_at: str, code: str) -> dict[str, Any]:
    current = collector_status(config)
    same_run = bool(payload.get("run_id") and payload.get("run_id") == current.get("run_id"))
    fresh = (current != previous and str(current.get("started_at") or "") >= started_at)
    # Old state can describe a wait, but must never be attached to an unrelated failed run.
    waiting = code == "chat_read_cooldown" and bool((current.get("read_recovery") or {}).get("next_retry_at"))
    merged = {**(current if same_run or fresh or waiting else {}), **payload}
    for key in ("read_recovery", "reconciliation"):
        if not merged.get(key) and (same_run or fresh or waiting) and current.get(key):
            merged[key] = current[key]
    evidence = merged.get("read_recovery") or chat_read_recovery.failure_evidence(
        payload, code, str(payload.get("error_detail") or ""))
    merged.update(status="failed", error_code=code, read_recovery=evidence)
    return {"incremental": merged, "read_recovery": evidence,
            "reconciliation": merged.get("reconciliation") or {}}


def run_incremental_sync(config_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    command = ["/usr/bin/python3", str(ROOT / "bin" / "sync_dingtalk_chat.py"),
               "--config", str(config_path), "--mode", "incremental", "--no-ledger"]
    timeout_seconds = int(config["dingtalk_chat"].get("timeout_seconds", 180)) * 30
    previous = collector_status(config)
    started_at = now_iso()
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout_seconds,
                                check=False, env={**os.environ, "LC_ALL": "en_US.UTF-8"})
    except subprocess.TimeoutExpired as exc:
        code = "incremental_sync_timeout"
        detail = collector_failure_detail(config, collector_payload(exc.stdout), previous, started_at, code)
        detail.update(phase="chat_incremental_sync", auto_repair="source_queue_preserved",
                      attempts=1, timeout_seconds=timeout_seconds)
        raise AutomationError(code, f"incremental DingTalk reconciliation exceeded {timeout_seconds} seconds",
                              notification_detail=detail) from exc
    except OSError as exc:
        raise AutomationError("incremental_sync_unavailable", "incremental collector could not start") from exc
    payload = collector_payload(result.stdout)
    if not payload:
        code = "incremental_invalid_json"
        raise AutomationError(code, "incremental sync did not return JSON",
                              notification_detail=collector_failure_detail(config, {}, previous, started_at, code))
    if result.returncode not in (0, 2) or payload.get("status") not in ("success", "partial"):
        code = str(payload.get("error_code") or f"incremental_exit_{result.returncode}")
        raise AutomationError(code, "incremental DingTalk reconciliation failed",
                              notification_detail=collector_failure_detail(config, payload, previous, started_at, code))
    return payload


def reconcile_events_from_incremental(config: dict[str, Any], incremental: dict[str, Any]) -> int:
    run_id = str(incremental.get("run_id") or "")
    if not UUID_RE.match(run_id):
        return 0
    connection = connect_database(config)
    try:
        cursor = connection.execute(
            """
            UPDATE event_inbox
            SET state='hydrated',last_attempt_at=?,last_error_code=NULL
            WHERE state IN ('received','staged','blocked')
              AND EXISTS (
                SELECT 1 FROM messages
                WHERE messages.message_id=event_inbox.message_id
                  AND messages.last_run_id=?
              )
            """,
            (now_iso(), run_id),
        )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        connection.close()


def query_event_status(config: dict[str, Any]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(dws_binary(config)), "event", "status", "--format", "json"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=event_environment(config),
        )
    except (OSError, subprocess.TimeoutExpired):
        append_log(runtime_paths(config)["log"], "live_acceptance_identity_unavailable", error_code="identity_timeout")
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def current_dingtalk_user_id(config: dict[str, Any]) -> Optional[str]:
    payload = query_event_status(config)
    identity = payload.get("identity") if isinstance(payload, dict) else None
    return str(identity.get("user_id") or "") if isinstance(identity, dict) and identity.get("user_id") else None


def live_subscription_receipts(event_status: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    """Confirm the ready subscriptions against the authenticated live bus.

    This is transport evidence only. The caller must also prove new external
    messages were persisted after ready, before using an empty listing fallback.
    """
    bus = event_status.get("bus") or {}
    entry, live = bus.get("entry") or {}, bus.get("live") or {}
    live_bus = live.get("bus") or {}
    identity = (event_status.get("identity") or {}).get("identity_hash")
    if (not identity or entry.get("identity_hash") != identity or live_bus.get("identity_hash") != identity
            or entry.get("state") != "running" or (live.get("source_state") or {}).get("state") != "connected"
            or entry.get("holder_pid") != live_bus.get("pid")):
        return {}
    try:
        bus_pid = int(live_bus.get("pid") or 0)
        if bus_pid <= 0:
            return {}
        os.kill(bus_pid, 0)
    except (OSError, ValueError, TypeError):
        return {}
    expected = {"direct": "user_im_message_receive_o2o_all", "group": "user_im_message_receive_group_all"}
    receipts = {}
    for consumer in live.get("consumers") or []:
        if not isinstance(consumer, dict) or not consumer.get("subscribe_id"):
            continue
        ref = sha256_text(str(consumer["subscribe_id"]))
        try:
            pid, received, dropped = (int(consumer.get(key) or 0) for key in ("pid", "received", "dropped"))
            if pid <= 0 or received <= 0 or dropped != 0:
                continue
            os.kill(pid, 0)
        except (OSError, ValueError, TypeError):
            continue
        for kind, event_key in expected.items():
            if ref == status.get(kind + "_subscription_ref") and event_key in (consumer.get("event_types") or []):
                receipts[kind] = {"subscription_ref": ref, "event_key": event_key, "consumer_pid": pid,
                                  "received": received, "dropped": dropped, "bus_pid": bus_pid}
    return receipts


def update_live_acceptance(config: dict[str, Any], incremental: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    connection = connect_database(config)
    try:
        status: dict[str, Any] = {}
        paths = runtime_paths(config)
        if paths["status"].is_file():
            try:
                loaded = json.loads(paths["status"].read_text(encoding="utf-8"))
                status = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                status = {}
        ready_at = status.get("listener_ready_at")
        ready = status.get("listener_state") == "ready" and status.get("direct_ready") and status.get("group_ready")
        event_status = query_event_status(config) if ready else {}
        user_id = str((event_status.get("identity") or {}).get("user_id") or "")
        incoming_by_kind = {"direct": False, "group": False}
        incoming_receipts = {}
        if ready and ready_at and user_id:
            for row in connection.execute(
                    "SELECT e.event_kind,e.event_id,e.received_at,m.create_time,m.sender_id,m.current_visible "
                    "FROM event_inbox e JOIN messages m ON m.message_id=e.message_id "
                    "WHERE e.received_at>=? AND e.state IN ('staged','hydrated') "
                    "ORDER BY e.received_at DESC LIMIT 2000", (ready_at,)):
                kind = row["event_kind"]
                if kind not in incoming_by_kind or not row["sender_id"] or row["sender_id"] == user_id or not row["current_visible"]:
                    continue
                try:
                    if chat_sync.parse_timestamp(row["create_time"]) < chat_sync.parse_timestamp(ready_at):
                        continue
                except (ValueError, TypeError):
                    continue
                incoming_by_kind[kind] = True
                incoming_receipts[kind] = {"event_ref": sha256_text(row["event_id"]), "received_at": row["received_at"]}
        incoming_seen = all(incoming_by_kind.values())
        subscriptions = event_status.get("subscriptions", [])
        observed_refs = set()
        stack = [subscriptions]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str):
                observed_refs.add(sha256_text(item))
        expected_refs = {status.get("direct_subscription_ref"), status.get("group_subscription_ref")}
        server_confirmed = None not in expected_refs and len(expected_refs) == 2 and expected_refs <= observed_refs
        live_receipts = live_subscription_receipts(event_status, status)
        # An empty provider list must not negate actual delivery. Require both
        # exact ready IDs, live matching consumers and newly persisted external
        # events. Neither a ready marker nor local history alone can pass.
        delivered_confirmed = (event_status.get("subscriptions") == [] and incoming_seen
                               and None not in expected_refs and len(expected_refs) == 2
                               and set(live_receipts) == {"direct", "group"})
        subscriptions_valid = server_confirmed or delivered_confirmed
        status["subscriptions_valid"] = subscriptions_valid
        status["subscription_verification"] = "server_listing" if server_confirmed else "live_delivery" if delivered_confirmed else "pending"
        status["server_subscription_listing"] = "confirmed" if server_confirmed else "empty" if event_status.get("subscriptions") == [] else "unconfirmed"
        status["live_subscription_receipts"] = live_receipts
        status["subscription_checked_at"] = now_iso()
        self_seen = bool(status.get("self_sent_reconciled_at"))
        if incremental and int(incremental.get("messages_inserted") or 0) + int(incremental.get("messages_updated") or 0) > 0:
            user_id = current_dingtalk_user_id(config)
            if user_id:
                row = connection.execute(
                    """
                    SELECT MAX(q.queued_at) AS seen_at FROM semantic_queue q
                    JOIN messages m ON m.message_id=q.message_id
                    WHERE q.queued_at>=? AND m.sender_id=?
                    """,
                    (str(incremental.get("started_at") or ""), user_id),
                ).fetchone()
                if row and row["seen_at"]:
                    self_seen = True
                    status["self_sent_reconciled_at"] = str(row["seen_at"])
        status["incoming_event_seen"] = incoming_seen
        status["incoming_by_kind"] = incoming_by_kind
        status["live_event_receipts"] = incoming_receipts
        status["live_acceptance"] = "passed" if ready and subscriptions_valid and incoming_seen else "live_acceptance_pending"
        if status["live_acceptance"] == "passed":
            status["live_acceptance_at"] = now_iso()
            resolve_alert(config, "listener-not-ready")
            import listener_manual_acceptance as manual_listener
            receipt_path = paths["status"].parent / "listener-manual-acceptance.json"
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text())
                if receipt.get("state") == "ready_acceptance_pending":
                    manual_listener.finish(config, receipt, "accepted")
        status_update(config, **status)
        return {"incoming_event_seen": incoming_seen, "self_sent_reconciled": self_seen, "status": status["live_acceptance"]}
    finally:
        connection.close()


def find_session_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("thread_id", "threadId", "session_id", "sessionId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and UUID_RE.match(candidate):
                return candidate
        for child in value.values():
            found = find_session_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_session_id(child)
            if found:
                return found
    return None


def subscription_only_environment() -> dict[str, str]:
    blocked = {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
    }
    environment = {key: value for key, value in os.environ.items() if key not in blocked}
    environment["LC_ALL"] = "en_US.UTF-8"
    # DeepSeek key is the default family since 2020-01-02 (MiMo paused).
    # Other retained families (GPT subscription / Claude) do not require it.
    try:
        environment["DEEPSEEK_API_KEY"] = deepseek_api_key()
    except AutomationError:
        environment.pop("DEEPSEEK_API_KEY", None)
    return environment


def record_pending_alerts_in_codex(config: dict[str, Any]) -> dict[str, Any]:
    """Compatibility entrypoint: local receipts replace the record-only model call."""
    connection = connect_database(config)
    try:
        rows = connection.execute(
            "SELECT alert_id,kind,severity,error_code,dedupe_key FROM automation_alerts "
            "WHERE status!='resolved' AND codex_status IN ('pending','failed') ORDER BY created_at LIMIT 100"
        ).fetchall()
        for row in rows:
            if row["dedupe_key"] != "incremental-partial":
                forward_sync_alert(config, str(row["kind"]), str(row["severity"]), str(row["error_code"] or "unknown_failure"))
            connection.execute("UPDATE automation_alerts SET codex_status='recorded' WHERE alert_id=?", (row["alert_id"],))
        connection.commit()
        return {"status": "recorded_local" if rows else "noop", "recorded": len(rows), "model_called": False}
    finally:
        connection.close()


def handoff_minutes_after_chat_model(paths: dict[str, Path], model_called: bool) -> dict[str, Any]:
    if not model_called:
        return {"status": "not_needed", "processed": 0, "done": 0, "deferred": 0, "failed": 0}
    try:
        handoff = syncv2.run_minutes_review_handoff()
        append_log(
            paths["log"],
            "minutes_review_handoff",
            status=str(handoff.get("status") or "unknown"),
            processed=int(handoff.get("processed") or 0),
            done=int(handoff.get("done") or 0),
            deferred=int(handoff.get("deferred") or 0),
            failed=int(handoff.get("failed") or 0),
        )
        return handoff
    except Exception:
        return {"status": "failed", "reason": "handoff_internal_error", "processed": 0, "done": 0, "deferred": 0, "failed": 1}


def run_hourly(
    config_path: Path,
    config: dict[str, Any],
    dry_run: bool = False,
    *,
    manual: bool = False,
    skip_reconcile: bool = False,
) -> dict[str, Any]:
    if dry_run:
        return preview_batch(config)
    if not manual and syncv2.MANUAL_SYNC_FLAG.is_file():
        return {"status": "deferred", "model_called": False, "messages": 0, "writes": 0, "reason": "manual_sync_priority"}
    lock = acquire_lock(HOURLY_LOCK)
    paths = runtime_paths(config)
    batch: Optional[dict[str, Any]] = None
    incremental: Optional[dict[str, Any]] = None
    model_called = False
    reused_proposal = False
    started_at = now_iso()
    try:
        recover_apply_journals(config)
        recover_stale_batches(config)
        if skip_reconcile and not manual:
            raise AutomationError("skip_reconcile_requires_manual", "only a manual drain may skip a repeated reconciliation")
        gate = chat_source_read_gate(config)
        # A drain is strictly local; auth/hold/cooldown must also block mget hydration.
        hydration = {"status": "skipped", "requested": 0, "hydrated": 0}
        reconciled_events = 0
        if skip_reconcile:
            previous = gate or collector_status(config)
            incremental = {"status": "skipped", "started_at": started_at, "read_attempted": False,
                           "run_id": previous.get("run_id"),
                           "read_recovery": previous.get("read_recovery") or {},
                           "reconciliation": previous.get("reconciliation") or {}}
        elif gate:
            incremental = gate
        else:
            try:
                incremental = run_incremental_sync(config_path, config)
            except (AutomationError, chat_sync.SyncError) as exc:
                # Only this source read is caught; validation/index/conflict gates remain fatal.
                detail = getattr(exc, "notification_detail", {}) or {}
                incremental = dict(detail.get("incremental") or getattr(exc, "receipt", {}) or {})
                incremental.update(status="failed", error_code=exc.code)
                incremental.setdefault("started_at", started_at)
                incremental["read_recovery"] = (detail.get("read_recovery") or incremental.get("read_recovery")
                                                or chat_read_recovery.failure_evidence({}, exc.code, str(exc)))
                incremental["reconciliation"] = detail.get("reconciliation") or incremental.get("reconciliation") or {}
            if incremental.get("status") == "success" and not incremental_reconcile_complete(incremental):
                incremental = {**incremental, "status": "partial", "error_code": "chat_reconcile_incomplete"}
            if incremental_reconcile_complete(incremental):
                reconciled_events = reconcile_events_from_incremental(config, incremental)
                try:
                    hydration = hydrate_pending_events(config)
                except (AutomationError, chat_sync.SyncError) as exc:
                    # mget is another source read, not an excuse to strand the stored queue.
                    hydration = {"status": "failed", "error_code": exc.code}
                    incremental = {**incremental, "status": "partial", "error_code": exc.code,
                                   "read_recovery": chat_read_recovery.failure_evidence({}, exc.code, str(exc))}
        source_incomplete = incremental.get("status") != "skipped" and not incremental_reconcile_complete(incremental)
        if incremental.get("status") in {"partial", "failed"}:
            raise_alert(
                config,
                "failure",
                "warning",
                "incremental-partial",
                "钉钉小时增量对账存在未完整会话",
                "incremental_partial",
            )
        elif incremental_reconcile_complete(incremental):
            resolve_alert(config, "incremental-partial")
        acceptance = update_live_acceptance(config, incremental)
        reusable = claim_reusable_proposal(config)
        proposal: Optional[dict[str, Any]] = None
        if reusable:
            batch, proposal = reusable
            reused_proposal = True
        else:
            waiting = eligible_queue_count(config)
            if waiting and semantic_model_called_this_slot(config) and not manual:
                alert_record = {"status": "skipped_manual", "recorded": 0} if manual else record_pending_alerts_in_codex(config)
                status_update(
                    config,
                    last_hourly_at=now_iso(),
                    last_hourly_status="partial" if source_incomplete else "deferred",
                    last_hourly_error=((incremental.get("read_recovery") or {}).get("error_code") or incremental.get("error_code") or "chat_reconcile_incomplete") if source_incomplete else None,
                    last_hourly_error_detail={"read_recovery": incremental.get("read_recovery") or {}, "reconciliation": incremental.get("reconciliation") or {}} if source_incomplete else None,
                    last_hourly_model_called=False,
                    last_incremental_status=str(incremental.get("status")),
                )
                append_log(paths["log"], "hourly_model_deferred", messages=waiting, reason="one_call_per_half_hour")
                syncv2.set_component_health("chat_memory", True)
                return {
                    "status": "partial" if source_incomplete else "deferred",
                    "model_called": False,
                    "messages": waiting,
                    "writes": 0,
                    "events_reconciled": reconciled_events,
                    "incremental": incremental,
                    "acceptance": acceptance,
                    "codex_alert_record": alert_record,
                }
            batch = prepare_batch(config)
        if batch is None:
            import dingtalk_prepared_recovery
            recovery = dingtalk_prepared_recovery.pending_summary(config)
            if recovery["pending_batches"]:
                raise AutomationError("prepared_recovery_blocked", "冲突消息仍保留，等待恢复证据或技术维护；未重复执行旧提交。",
                                      notification_detail={"phase": "chat_memory", "prepared_recovery": recovery})
            decision_registration = register_pending_decisions(config_path)
            alert_record = {"status": "skipped_manual", "recorded": 0} if manual else record_pending_alerts_in_codex(config)
            status_update(
                config,
                last_hourly_at=now_iso(),
                last_hourly_status="partial" if source_incomplete else "noop",
                last_hourly_error=((incremental.get("read_recovery") or {}).get("error_code") or incremental.get("error_code") or "chat_reconcile_incomplete") if source_incomplete else None,
                last_hourly_error_detail={"read_recovery": incremental.get("read_recovery") or {}, "reconciliation": incremental.get("reconciliation") or {}} if source_incomplete else None,
                last_hourly_model_called=False,
                last_incremental_status=str(incremental.get("status")),
            )
            append_log(paths["log"], "hourly_noop", hydration_status=hydration["status"], incremental_status=incremental["status"])
            syncv2.set_component_health("chat_memory", True)
            return {
                "status": "partial" if source_incomplete else "noop",
                "model_called": False,
                "hydration": hydration,
                "incremental": incremental,
                "events_reconciled": reconciled_events,
                "acceptance": acceptance,
                "codex_alert_record": alert_record,
                "decision_registration": decision_registration,
            }
        if proposal is None:
            model_called = True
            proposal = invoke_codex(config, batch)
        applied = apply_proposal_with_recovery(config, batch, proposal)
        decision_registration = register_pending_decisions(config_path)
        alert_record = {"status": "skipped_manual", "recorded": 0} if manual else record_pending_alerts_in_codex(config)
        status_update(
            config,
            last_hourly_at=now_iso(),
            last_hourly_status="partial" if source_incomplete else "success",
            last_hourly_error=((incremental.get("read_recovery") or {}).get("error_code") or incremental.get("error_code") or "chat_reconcile_incomplete") if source_incomplete else None,
            last_hourly_error_detail={"read_recovery": incremental.get("read_recovery") or {}, "reconciliation": incremental.get("reconciliation") or {}} if source_incomplete else None,
            last_hourly_model_called=model_called,
            last_batch_id=str(batch["batch_id"]),
            last_incremental_status=str(incremental.get("status")),
        )
        resolve_alert(config, "hourly-failed")
        syncv2.set_component_health("chat_memory", True)
        append_log(
            paths["log"],
            "hourly_completed",
            batch_id=str(batch["batch_id"]),
            messages=batch["message_count"],
            writes=applied["writes"],
            conflicts=len(applied["conflicts"]),
            reused_proposal=reused_proposal,
        )
        return {
            "status": "partial" if source_incomplete else "success",
            "model_called": model_called,
            "reused_proposal": reused_proposal,
            "batch_id": str(batch["batch_id"]),
            "messages": int(batch["message_count"]),
            "writes": int(applied["writes"]),
            "target_memory_ids": applied["target_memory_ids"],
            "conflicts": applied["conflicts"],
            "incremental": incremental,
            "events_reconciled": reconciled_events,
            "acceptance": acceptance,
            "codex_alert_record": alert_record,
            "decision_registration": decision_registration,
        }
    except Exception as exc:
        code = exc.code if isinstance(exc, (AutomationError, chat_sync.SyncError)) else type(exc).__name__
        failure_detail = failure_notification_detail(exc)
        if incremental:
            failure_detail["incremental"] = safe_hourly_receipt({"incremental": incremental})["incremental"]
            exc.receipt = {**getattr(exc, "receipt", {}), "incremental": failure_detail["incremental"]}
            if isinstance(exc, AutomationError):
                exc.notification_detail.update(failure_detail)
        if batch:
            commit_guard.record_failure(paths["runs"] / str(batch["batch_id"]), code, failure_detail, stage="hourly_failure")
            release_batch_failure(config, str(batch["batch_id"]), code)
        raise_alert(config, "failure", "critical", "hourly-failed", "钉钉业务记忆小时处理失败", code)
        status_update(
            config,
            last_hourly_at=now_iso(),
            last_hourly_status="deferred" if code == "validation_inputs_changed" else "failed",
            last_hourly_error=code,
            last_hourly_error_detail=failure_detail,
            last_hourly_model_called=model_called,
        )
        append_log(
            paths["log"],
            "hourly_failed",
            error_code=code,
            started_at=started_at,
            phase=failure_detail.get("phase"),
            auto_repair=failure_detail.get("auto_repair"),
            attempts=failure_detail.get("attempts"),
            cause_category=failure_detail.get("cause_category"),
            cause_fingerprint=failure_detail.get("cause_fingerprint"),
            original_error_code=failure_detail.get("original_error_code"),
            rollback_error_code=failure_detail.get("rollback_error_code"),
        )
        syncv2.set_component_health(
            "chat_memory",
            False,
            str(code),
            safe_detail=failure_detail,
        )
        raise
    finally:
        lock.close()
        handoff_minutes_after_chat_model(paths, model_called)


def run_selftest() -> int:
    test_script = ROOT / "bin" / "selftest_dingtalk_memory_automation.py"
    if not test_script.is_file():
        raise AutomationError("selftest_missing", "automation selftest script is missing")
    result = subprocess.run(
        ["/usr/bin/python3", str(test_script)],
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


def safe_hourly_receipt(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") == "dry_run":
        return result
    incremental = result.get("incremental") if isinstance(result.get("incremental"), dict) else {}
    acceptance = result.get("acceptance") if isinstance(result.get("acceptance"), dict) else {}
    alert_record = result.get("codex_alert_record") if isinstance(result.get("codex_alert_record"), dict) else {}
    decision_registration = result.get("decision_registration") if isinstance(result.get("decision_registration"), dict) else {}
    return {
        "status": str(result.get("status") or "unknown"),
        "model_called": bool(result.get("model_called")),
        "reused_proposal": bool(result.get("reused_proposal")),
        "messages": int(result.get("messages") or 0),
        "writes": int(result.get("writes") or 0),
        "conflict_count": len(result.get("conflicts") or []) if isinstance(result.get("conflicts"), list) else 0,
        "events_reconciled": int(result.get("events_reconciled") or 0),
        "incremental": {
            "status": incremental.get("status"),
            "run_id": incremental.get("run_id"),
            "read_attempted": incremental.get("read_attempted"),
            "window_start": incremental.get("window_start"),
            "window_end": incremental.get("window_end"),
            "receipt_export_error": incremental.get("receipt_export_error"),
            "read_recovery": incremental.get("read_recovery") or {},
            "reconciliation": incremental.get("reconciliation") or {},
            "error_code": incremental.get("error_code"),
            "conversations_discovered": incremental.get("conversations_discovered"),
            "conversations_complete": incremental.get("conversations_complete"),
            "conversations_incomplete": incremental.get("conversations_incomplete"),
            "messages_inserted": int(incremental.get("messages_inserted") or 0),
            "messages_updated": int(incremental.get("messages_updated") or 0),
            "messages_hidden": int(incremental.get("messages_hidden") or 0),
        },
        "acceptance": {
            "status": acceptance.get("status"),
            "incoming_event_seen": bool(acceptance.get("incoming_event_seen")),
            "self_sent_reconciled": bool(acceptance.get("self_sent_reconciled")),
        },
        "codex_alert_record": {
            "status": alert_record.get("status"),
            "recorded": int(alert_record.get("recorded") or 0),
        },
        "decision_registration": {
            "created": int((decision_registration.get("registered") or {}).get("created") or 0)
            if isinstance(decision_registration.get("registered"), dict)
            else 0,
            "updated": int((decision_registration.get("registered") or {}).get("updated") or 0)
            if isinstance(decision_registration.get("registered"), dict)
            else 0,
        },
    }


def attachment_entry(config: dict[str, Any], command: str, adopt: bool = False) -> dict[str, Any]:
    section = config.get("dingtalk_attachment_handoff") or {}
    if section.get("enabled") is not True:
        return {"status": "disabled"}
    directory = ROOT / "lib" / "dingtalk-attachments-v1"
    manifest_path = directory / "release-manifest.json"
    if sha256_path(manifest_path) != section.get("release_manifest_sha256"):
        raise AutomationError("attachment_release_changed", "attachment release manifest changed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if Path(name).name != name or sha256_path(directory / name) != expected:
            raise AutomationError("attachment_release_changed", "attachment code changed")
    sys.path.insert(0, str(directory))
    try:
        import attachment_controller
        if command == "attachments-enable":
            return attachment_controller.enable(config, sys.modules[__name__])
        if command == "attachments-status":
            database = Path(config["external_root"]) / "dingtalk-attachments" / "requests.sqlite"
            if not database.is_file(): return {"status": "not_initialized"}
            connection = sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True)
            try:
                counts = connection.execute("SELECT stage,status,count(*) FROM requests GROUP BY stage,status").fetchall()
                return {"enabled": (database.parent / "activation.json").is_file(), "counts": [{"stage":a,"status":b,"count":c} for a,b,c in counts],
                        "quick_check":connection.execute("PRAGMA quick_check").fetchone()[0]}
            finally: connection.close()
        approved = None
        if adopt:
            acceptance = Path(config["external_root"]) / "dingtalk-attachments" / "accepted-canary.json"
            approved = json.loads(acceptance.read_text(encoding="utf-8"))["folder"]
        return attachment_controller.once(config, sys.modules[__name__],
            initialize=command == "attachments-init", adopt=approved)
    except attachment_controller.Rejected as exc:
        code = str(exc)
        if not re.fullmatch(r"[a-zA-Z0-9_]{1,100}", code): code = "attachment_blocked"
        raise AutomationError(code, "attachment handoff safely stopped") from exc
    finally:
        sys.path.remove(str(directory))


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DingTalk to BUSINESS business-memory automation")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("listener", help="run the two long-lived DingTalk event consumers")
    hourly = subparsers.add_parser("hourly", help="reconcile and process at most one semantic batch")
    hourly.add_argument("--dry-run", action="store_true", help="preview counts without network, model, or memory writes")
    hourly.add_argument("--manual", action="store_true", help="run an immediate user-requested incremental cycle")
    hourly.add_argument("--skip-reconcile", action="store_true", help="manual queue drain after the first reconciliation")
    status = subparsers.add_parser("status", help="show privacy-safe automation status")
    status.add_argument("--json", action="store_true", help="emit JSON")
    subparsers.add_parser("selftest", help="run offline automation acceptance tests")
    subparsers.add_parser("attachments-status", help="read attachment handoff state")
    subparsers.add_parser("attachments-init", help="initialize local metadata baseline only")
    subparsers.add_parser("attachments-enable", help="activate only after real governed canary commit")
    attachment = subparsers.add_parser("attachments-once", help="one bounded existing-scheduler handoff cycle")
    attachment.add_argument("--adopt-approved", action="store_true", help="apply the exact approved existing canary")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    if args.command == "selftest":
        return run_selftest()
    try:
        config = load_config(args.config)
        if args.command.startswith("attachments-"):
            print(json.dumps(attachment_entry(config, args.command, bool(getattr(args, "adopt_approved", False))), ensure_ascii=False))
            return 0
        for path in runtime_paths(config).values():
            if path.suffix == "" and path.name in ("runs", "codex-alerts"):
                ensure_private_dir(path)
        if args.command == "listener":
            return run_listener(config)
        if args.command == "hourly":
            result = run_hourly(
                args.config,
                config,
                dry_run=bool(args.dry_run),
                manual=bool(args.manual),
                skip_reconcile=bool(args.skip_reconcile),
            )
            print(json.dumps(safe_hourly_receipt(result), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "status":
            result = safe_status_snapshot(config)
            print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None, sort_keys=True))
            return 0
        raise AutomationError("unknown_command", "unknown automation command")
    except (AutomationError, chat_sync.SyncError) as exc:
        # This output is consumed by LaunchAgent logs. Keep it deliberately
        # metadata-only; detailed diagnostics remain in the private database.
        if (
            args.command == "hourly"
            and not bool(getattr(args, "manual", False))
            and exc.code == "already_running"
        ):
            print(json.dumps({"status": "deferred", "reason": "already_running"}, ensure_ascii=False))
            return 0
        detail = getattr(exc, "notification_detail", {}) or {}
        receipt = getattr(exc, "receipt", {}) or {}
        incremental = detail.get("incremental") or receipt.get("incremental") or receipt
        failure = {"status": "failed", "error_code": exc.code}
        if incremental:
            failure["incremental"] = safe_hourly_receipt({"incremental": incremental})["incremental"]
        print(json.dumps(failure, ensure_ascii=False))
        return 1
    except Exception as exc:
        failure = {"status": "failed", "error_code": type(exc).__name__}
        receipt = getattr(exc, "receipt", {})
        if receipt.get("incremental"):
            failure["incremental"] = safe_hourly_receipt(receipt)["incremental"]
        print(json.dumps(failure, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
