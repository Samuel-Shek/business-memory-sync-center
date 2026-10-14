#!/usr/bin/env python3
"""Read DingTalk chat history into a private local source database.

The source database is deliberately outside Obsidian/iCloud.  Obsidian gets
only an aggregate, privacy-safe receipt; business conclusions are curated as
separate source-linked memory notes.  The collector never sends, edits, marks,
downloads, or recalls a DingTalk message.
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
import re
import sqlite3
import time
import chat_read_recovery
import chat_reconcile_checkpoint as checkpoint
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
LOCK_PATH = ROOT / "state" / "dingtalk_chat_sync.lock"
SCHEMA_VERSION = 3

SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(password|passwd|pwd|passcode|secret|token|appsecret|client[_-]?secret|"
    r"api[_-]?key|密码|口令|验证码|校验码)(\s*[:=：]\s*)([^\s,;，；]{4,})"
)
OTP_RE = re.compile(r"(?i)((?:验证码|校验码|otp|code)\s*(?:是|为|[:=：])?\s*)(\d{4,8})")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
PII_ASSIGNMENT_RE = re.compile(
    r"(?i)(身份证(?:号)?|社保(?:电脑)?号|公积金(?:账号|号)?|银行卡(?:号)?|银行账号|"
    r"手机号|手机号码)(\s*[:=：]?\s*)([A-Za-z0-9-]{6,32})"
)
STANDALONE_LONG_NUMBER_RE = re.compile(r"^\s*\d{8,24}\s*$")
HIGH_ENTROPY_PAIR_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9._-]{1,24})\s+(\S{8,64})\s*$")
URL_RE = re.compile(r"(?i)(?:https?|dingtalk)://[^\s)]+")
SENSITIVE_PROMPT_RE = re.compile(
    r"(?i)(?:(?:密码|口令|验证码|校验码|token|secret|api[_-]?key|服务器账号|账号|"
    r"社保电脑号|公积金号|身份证号|银行卡号).{0,12}(?:是多少|是什么|在哪|有吗|呢|"
    r"发(?:我|一下)?|给我|提供(?:一下)?|[?？])|"
    r"(?:请|麻烦)?(?:发|给|提供).{0,12}(?:密码|口令|验证码|校验码|token|secret|"
    r"api[_-]?key|服务器账号|账号|社保电脑号|公积金号|身份证号|银行卡号))"
)
SENSITIVE_RESOURCE_KEY_RE = re.compile(r"(?i)(token|secret|password|signature|downloadurl|signedurl)")


class SyncError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_log(path: Path, event: str, **fields: Any) -> None:
    ensure_private_dir(path.parent)
    record = {"at": now_iso(), "event": event, **fields}
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (canonical_json(record) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def acquire_lock() -> Any:
    ensure_private_dir(LOCK_PATH.parent)
    handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise SyncError("already_running", "another DingTalk chat sync is already running") from exc
    return handle


def redact_text(value: Any, max_chars: int, force_sensitive_reply: bool = False) -> tuple[str, int]:
    text = value if isinstance(value, str) else ""
    if force_sensitive_reply and text.strip():
        return "[敏感回复已脱敏]", 1
    redactions = 0

    def assignment(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group(1)}{match.group(2)}[已脱敏]"

    def otp(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group(1)}[已脱敏]"

    def pii(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group(1)}{match.group(2)}[已脱敏]"

    text = PRIVATE_KEY_RE.sub(lambda _match: "[私钥内容已脱敏]", text)
    if "[私钥内容已脱敏]" in text:
        redactions += 1
    text = JWT_RE.sub(lambda _match: "[Token已脱敏]", text)
    redactions += len(JWT_RE.findall(value if isinstance(value, str) else ""))
    text = SENSITIVE_ASSIGNMENT_RE.sub(assignment, text)
    text = OTP_RE.sub(otp, text)
    text = PII_ASSIGNMENT_RE.sub(pii, text)
    if STANDALONE_LONG_NUMBER_RE.fullmatch(text):
        text = "[长号码已脱敏]"
        redactions += 1
    high_entropy_pair = HIGH_ENTROPY_PAIR_RE.fullmatch(text)
    if high_entropy_pair:
        candidate = high_entropy_pair.group(2)
        character_classes = sum(
            bool(pattern.search(candidate))
            for pattern in (re.compile(r"[A-Z]"), re.compile(r"[a-z]"), re.compile(r"\d"), re.compile(r"[^A-Za-z0-9]"))
        )
        if character_classes >= 3:
            text = f"{high_entropy_pair.group(1)} [疑似口令已脱敏]"
            redactions += 1
    url_count = len(URL_RE.findall(text))
    if url_count:
        text = URL_RE.sub("[链接已省略]", text)
        redactions += url_count
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[超长消息已截断；原文及其指纹未保留]"
        redactions += 1
    return text, redactions


def sanitize_resource(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[嵌套过深已省略]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if SENSITIVE_RESOURCE_KEY_RE.search(key):
                result[key] = "[已脱敏]"
            else:
                result[key] = sanitize_resource(raw_value, depth + 1)
        return result
    if isinstance(value, list):
        return [sanitize_resource(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value if len(value) <= 2048 else value[:2048] + "[已截断]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2048]


def safe_error(stderr: str, returncode: int) -> tuple[str, str]:
    code = f"dws_exit_{returncode}"
    detail = "DingTalk command returned a non-zero result"
    try:
        payload = json.loads(stderr)
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            code = str(error.get("reason") or error.get("operation") or code)[:120]
            detail = str(error.get("message") or detail)[:300]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return code, detail


def migrate_sensitive_fingerprints(connection: sqlite3.Connection) -> None:
    """Replace legacy hashes derived from sensitive plaintext with safe hashes."""
    rows = connection.execute(
        """
        SELECT message_id,conversation_id,create_time,sender_id,text_redacted,
               original_text_sha256,content_sha256,resource_refs_json,
               resource_refs_sha256,redaction_count,last_seen_at
        FROM messages
        """
    ).fetchall()
    for row in rows:
        safe_text_hash = sha256_bytes(str(row["text_redacted"]).encode("utf-8"))
        safe_refs_hash = sha256_bytes(str(row["resource_refs_json"]).encode("utf-8"))
        safe_content_hash = sha256_bytes(
            canonical_json(
                {
                    "conversationId": str(row["conversation_id"]),
                    "createTime": str(row["create_time"]),
                    "messageId": str(row["message_id"]),
                    "resourceRefsSha256": safe_refs_hash,
                    "senderId": str(row["sender_id"]),
                    "safeTextSha256": safe_text_hash,
                }
            ).encode("utf-8")
        )
        is_redacted = int(row["redaction_count"]) > 0
        refs_hash_changed = str(row["resource_refs_sha256"]) != safe_refs_hash
        content_hash_changed = str(row["content_sha256"]) != safe_content_hash
        original_hash_unsafe = is_redacted and bool(str(row["original_text_sha256"]))
        if refs_hash_changed or content_hash_changed or original_hash_unsafe:
            connection.execute(
                """
                UPDATE messages
                SET original_text_sha256=?,content_sha256=?,resource_refs_sha256=?
                WHERE message_id=?
                """,
                (
                    "" if is_redacted else str(row["original_text_sha256"]),
                    safe_content_hash,
                    safe_refs_hash,
                    str(row["message_id"]),
                ),
            )
            connection.execute(
                """
                INSERT INTO message_versions(
                  message_id,content_sha256,observed_at,text_redacted,original_text_sha256,
                  resource_refs_json,resource_refs_sha256,redaction_count
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(message_id,content_sha256) DO UPDATE SET
                  text_redacted=excluded.text_redacted,
                  original_text_sha256=excluded.original_text_sha256,
                  resource_refs_json=excluded.resource_refs_json,
                  resource_refs_sha256=excluded.resource_refs_sha256,
                  redaction_count=excluded.redaction_count
                """,
                (
                    str(row["message_id"]), safe_content_hash, str(row["last_seen_at"]),
                    str(row["text_redacted"]), "" if is_redacted else str(row["original_text_sha256"]),
                    str(row["resource_refs_json"]), safe_refs_hash, int(row["redaction_count"]),
                ),
            )
        if is_redacted or refs_hash_changed:
            connection.execute(
                "DELETE FROM message_versions WHERE message_id=? AND content_sha256<>?",
                (str(row["message_id"]), safe_content_hash),
            )
    connection.execute(
        """
        DELETE FROM message_versions
        WHERE redaction_count > 0
          AND NOT EXISTS (
            SELECT 1 FROM messages
            WHERE messages.message_id=message_versions.message_id
              AND messages.redaction_count > 0
              AND messages.original_text_sha256=''
              AND messages.content_sha256=message_versions.content_sha256
          )
        """
    )


def run_dws(binary: Path, arguments: list[str], timeout: int) -> tuple[dict[str, Any], int, str, str]:
    command = [str(binary), *arguments, "--format", "json"]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "en_US.UTF-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError("dws_timeout", f"DingTalk read timed out after {timeout} seconds") from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        code, detail = safe_error(result.stderr, result.returncode)
        raise SyncError(code, detail) from exc
    if not isinstance(payload, dict):
        raise SyncError("dws_invalid_json", "DingTalk returned a non-object JSON payload")
    code, detail = safe_error(result.stderr, result.returncode)
    return payload, result.returncode, code, detail


def message_payload_complete(payload: dict[str, Any], returncode: int) -> bool:
    return (
        returncode == 0
        and payload.get("complete") is True
        and payload.get("partial") is not True
        and payload.get("hasMore") is not True
        and payload.get("truncated") is not True
        and int(payload.get("failedCount") or 0) == 0
        and not payload.get("failures")
        and isinstance(payload.get("messages"), list)
    )


def read_messages_with_recovery(binary, arguments, timeout, log_path, run_id, conversation_id):
    started = time.monotonic()
    try:
        result = run_dws(binary, arguments, timeout)
    except SyncError as exc:
        result = ({"complete": False, "partial": True, "messages": [], "failedCount": 1,
                   "stopReason": "read_failure"}, 1, exc.code, exc.message)
    payload, returncode, code, detail = result
    if message_payload_complete(payload, returncode):
        return result
    evidence = chat_read_recovery.failure_evidence(payload, code, detail)
    evidence.update(phase="messages", elapsed_ms=int((time.monotonic()-started)*1000),
                    pages_fetched=int(payload.get("pagesFetched") or 0),
                    failed_count=int(payload.get("failedCount") or 0))
    # One provider request only. Durable scheduling owns all later retries.
    append_log(log_path, "chat_read_failure", run_id=run_id,
               conversation_ref=sha256_bytes(conversation_id.encode()), **evidence)
    payload = dict(payload)
    payload["read_recovery"] = evidence
    return payload, returncode, evidence["error_code"], "消息读取未完整；脱敏失败证据已保留。"


def should_try_range_fallback(
    mode: str,
    historical_backfill: bool,
    payload: dict[str, Any],
    returncode: int,
) -> bool:
    return (
        (mode == "full" or historical_backfill)
        and (payload.get("read_recovery") or {}).get("error_code") not in {"chat_read_auth_required", "chat_read_blocked"}
        and not (payload.get("read_recovery") or {}).get("retry_after_seconds")
        and not message_payload_complete(payload, returncode)
        and (payload.get("hasMore") is True or int(payload.get("failedCount") or 0) > 0)
    )


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split(None, 1)[0]
    if name not in table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def migrate_schema_v2(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise SyncError("database_schema_newer", f"database schema v{version} is newer than supported v{SCHEMA_VERSION}")

    ensure_column(connection, "sync_runs", "mode TEXT NOT NULL DEFAULT 'full'")
    ensure_column(connection, "sync_runs", "window_start TEXT")
    ensure_column(connection, "sync_runs", "window_end TEXT")
    ensure_column(connection, "sync_runs", "messages_hidden INTEGER NOT NULL DEFAULT 0")
    ensure_column(connection, "conversation_scans", "scan_mode TEXT NOT NULL DEFAULT 'full'")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_inbox (
          event_id TEXT PRIMARY KEY,
          event_kind TEXT NOT NULL CHECK(event_kind IN ('direct','group')),
          message_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          event_time TEXT,
          received_at TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('received','staged','hydrated','blocked')),
          attempt_count INTEGER NOT NULL DEFAULT 0,
          last_attempt_at TEXT,
          last_error_code TEXT
        );

        CREATE TABLE IF NOT EXISTS semantic_batches (
          batch_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          started_at TEXT,
          completed_at TEXT,
          status TEXT NOT NULL CHECK(status IN ('prepared','model_complete','applied','rolled_back','failed','dry_run')),
          message_count INTEGER NOT NULL DEFAULT 0,
          text_chars INTEGER NOT NULL DEFAULT 0,
          proposal_sha256 TEXT,
          model_name TEXT,
          model_effort TEXT,
          error_code TEXT,
          error_detail TEXT
        );

        CREATE TABLE IF NOT EXISTS semantic_queue (
          queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id TEXT NOT NULL REFERENCES messages(message_id),
          content_sha256 TEXT NOT NULL,
          change_kind TEXT NOT NULL CHECK(change_kind IN ('insert','update','hidden','visible_again')),
          queued_at TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('pending','claimed','applied','no_business_change','deferred','failed')),
          batch_id TEXT REFERENCES semantic_batches(batch_id),
          attempt_count INTEGER NOT NULL DEFAULT 0,
          last_attempt_at TEXT,
          last_error_code TEXT,
          applied_at TEXT,
          UNIQUE(message_id, content_sha256, change_kind)
        );

        CREATE TABLE IF NOT EXISTS batch_messages (
          batch_id TEXT NOT NULL REFERENCES semantic_batches(batch_id),
          queue_id INTEGER NOT NULL REFERENCES semantic_queue(queue_id),
          ordinal INTEGER NOT NULL,
          source_token TEXT NOT NULL,
          PRIMARY KEY(batch_id, queue_id),
          UNIQUE(batch_id, source_token)
        );

        CREATE TABLE IF NOT EXISTS automation_alerts (
          alert_id TEXT PRIMARY KEY,
          dedupe_key TEXT NOT NULL UNIQUE,
          kind TEXT NOT NULL CHECK(kind IN ('failure','conflict')),
          severity TEXT NOT NULL CHECK(severity IN ('warning','critical')),
          title TEXT NOT NULL,
          error_code TEXT,
          created_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          last_notified_at TEXT,
          notify_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK(status IN ('pending','notified','resolved')),
          codex_status TEXT NOT NULL CHECK(codex_status IN ('pending','recorded','failed')),
          codex_session_id TEXT,
          resolved_at TEXT
        );

        CREATE INDEX IF NOT EXISTS event_inbox_state ON event_inbox(state, received_at);
        CREATE INDEX IF NOT EXISTS semantic_queue_state ON semantic_queue(state, queued_at, queue_id);
        CREATE INDEX IF NOT EXISTS semantic_queue_message ON semantic_queue(message_id, content_sha256);
        CREATE INDEX IF NOT EXISTS semantic_batches_status ON semantic_batches(status, created_at);
        CREATE TABLE IF NOT EXISTS recovery_resolutions (
          old_batch_id TEXT PRIMARY KEY, child_batch_id TEXT NOT NULL UNIQUE,
          approval_sha256 TEXT NOT NULL, state TEXT NOT NULL,
          completion_sha256 TEXT, created_at TEXT NOT NULL, completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS automation_alerts_status ON automation_alerts(status, last_seen_at);
        """
    )
    ensure_column(connection, "semantic_batches", "input_manifest_sha256 TEXT")
    if version < 2:
        connection.execute("PRAGMA user_version = 2")


def migrate_schema_v3(connection: sqlite3.Connection) -> None:
    """Allow an abandoned run to have an explicit interrupted terminal state."""
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version >= 3:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE sync_runs_v3 (
              run_id TEXT PRIMARY KEY,
              started_at TEXT NOT NULL,
              completed_at TEXT,
              status TEXT NOT NULL CHECK(status IN ('running','success','partial','failed','interrupted')),
              conversations_discovered INTEGER NOT NULL DEFAULT 0,
              conversations_complete INTEGER NOT NULL DEFAULT 0,
              conversations_incomplete INTEGER NOT NULL DEFAULT 0,
              messages_returned INTEGER NOT NULL DEFAULT 0,
              messages_inserted INTEGER NOT NULL DEFAULT 0,
              messages_updated INTEGER NOT NULL DEFAULT 0,
              redactions INTEGER NOT NULL DEFAULT 0,
              resource_refs INTEGER NOT NULL DEFAULT 0,
              error_code TEXT,
              error_detail TEXT,
              mode TEXT NOT NULL DEFAULT 'full',
              window_start TEXT,
              window_end TEXT,
              messages_hidden INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sync_runs_v3(
              run_id,started_at,completed_at,status,conversations_discovered,
              conversations_complete,conversations_incomplete,messages_returned,
              messages_inserted,messages_updated,redactions,resource_refs,error_code,
              error_detail,mode,window_start,window_end,messages_hidden
            )
            SELECT run_id,started_at,completed_at,status,conversations_discovered,
              conversations_complete,conversations_incomplete,messages_returned,
              messages_inserted,messages_updated,redactions,resource_refs,error_code,
              error_detail,mode,window_start,window_end,messages_hidden
            FROM sync_runs
            """
        )
        connection.execute("DROP TABLE sync_runs")
        connection.execute("ALTER TABLE sync_runs_v3 RENAME TO sync_runs")
        connection.execute("PRAGMA user_version=3")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise SyncError("database_foreign_key_violation", "chat database migration failed integrity checks")


def connect_database(path: Path) -> sqlite3.Connection:
    ensure_private_dir(path.parent)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA secure_delete = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sync_runs (
          run_id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          completed_at TEXT,
          status TEXT NOT NULL CHECK(status IN ('running','success','partial','failed')),
          conversations_discovered INTEGER NOT NULL DEFAULT 0,
          conversations_complete INTEGER NOT NULL DEFAULT 0,
          conversations_incomplete INTEGER NOT NULL DEFAULT 0,
          messages_returned INTEGER NOT NULL DEFAULT 0,
          messages_inserted INTEGER NOT NULL DEFAULT 0,
          messages_updated INTEGER NOT NULL DEFAULT 0,
          redactions INTEGER NOT NULL DEFAULT 0,
          resource_refs INTEGER NOT NULL DEFAULT 0,
          error_code TEXT,
          error_detail TEXT
        );

        CREATE TABLE IF NOT EXISTS conversations (
          conversation_id TEXT PRIMARY KEY,
          conversation_name TEXT NOT NULL,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          last_scan_at TEXT NOT NULL,
          last_complete_scan_at TEXT,
          last_scan_status TEXT NOT NULL,
          last_error_code TEXT,
          last_error_detail TEXT,
          last_message_at TEXT,
          visible_message_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS messages (
          message_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
          create_time TEXT NOT NULL,
          sender_id TEXT NOT NULL,
          sender_name TEXT NOT NULL,
          text_redacted TEXT NOT NULL,
          original_text_sha256 TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          resource_refs_json TEXT NOT NULL,
          resource_refs_sha256 TEXT NOT NULL,
          redaction_count INTEGER NOT NULL,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          last_run_id TEXT NOT NULL REFERENCES sync_runs(run_id),
          current_visible INTEGER NOT NULL CHECK(current_visible IN (0,1))
        );

        CREATE TABLE IF NOT EXISTS message_versions (
          message_id TEXT NOT NULL REFERENCES messages(message_id),
          content_sha256 TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          text_redacted TEXT NOT NULL,
          original_text_sha256 TEXT NOT NULL,
          resource_refs_json TEXT NOT NULL,
          resource_refs_sha256 TEXT NOT NULL,
          redaction_count INTEGER NOT NULL,
          PRIMARY KEY(message_id, content_sha256)
        );

        CREATE TABLE IF NOT EXISTS conversation_scans (
          run_id TEXT NOT NULL REFERENCES sync_runs(run_id),
          conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
          started_at TEXT NOT NULL,
          completed_at TEXT NOT NULL,
          status TEXT NOT NULL,
          returned_count INTEGER NOT NULL,
          complete INTEGER NOT NULL CHECK(complete IN (0,1)),
          partial INTEGER NOT NULL CHECK(partial IN (0,1)),
          has_more INTEGER NOT NULL CHECK(has_more IN (0,1)),
          pages_fetched INTEGER NOT NULL,
          failed_count INTEGER NOT NULL,
          truncated INTEGER NOT NULL CHECK(truncated IN (0,1)),
          stop_reason TEXT,
          error_code TEXT,
          error_detail TEXT,
          normalization_failures INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(run_id, conversation_id)
        );

        CREATE INDEX IF NOT EXISTS messages_conversation_time
          ON messages(conversation_id, create_time);
        CREATE INDEX IF NOT EXISTS messages_visible
          ON messages(current_visible, create_time);
        CREATE INDEX IF NOT EXISTS scans_status
          ON conversation_scans(status, completed_at);
        """
    )
    prior_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if prior_version == 0:
        connection.execute("PRAGMA user_version = 1")
    migrate_schema_v2(connection)
    migrate_schema_v3(connection)
    if prior_version < SCHEMA_VERSION:
        migrate_sensitive_fingerprints(connection)
    connection.commit()
    path.chmod(0o600)
    return connection


def as_bool(value: Any) -> int:
    return 1 if value is True else 0


def normalize_message(
    raw: Any,
    conversation_id: str,
    max_chars: int,
    force_sensitive_reply: bool = False,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("message is not an object")
    message_id = str(raw.get("messageId") or "").strip()
    create_time = str(raw.get("createTime") or "").strip()
    returned_conversation = str(raw.get("conversationId") or conversation_id).strip()
    if not message_id or not create_time or returned_conversation != conversation_id:
        raise ValueError("message identity is incomplete or mismatched")
    original_text = raw.get("text") if isinstance(raw.get("text"), str) else ""
    redacted_text, redaction_count = redact_text(original_text, max_chars, force_sensitive_reply)
    original_refs = raw.get("resourceRefs") if isinstance(raw.get("resourceRefs"), list) else []
    sanitized_refs = sanitize_resource(original_refs)
    refs_json = canonical_json(sanitized_refs)
    safe_text_hash = sha256_bytes(redacted_text.encode("utf-8"))
    original_text_hash = "" if redaction_count else sha256_bytes(original_text.encode("utf-8"))
    refs_hash = sha256_bytes(refs_json.encode("utf-8"))
    content_hash = sha256_bytes(
        canonical_json(
            {
                "conversationId": conversation_id,
                "createTime": create_time,
                "messageId": message_id,
                "resourceRefsSha256": refs_hash,
                "senderId": str(raw.get("senderId") or ""),
                "safeTextSha256": safe_text_hash,
            }
        ).encode("utf-8")
    )
    return {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "create_time": create_time,
        "sender_id": str(raw.get("senderId") or ""),
        "sender_name": str(raw.get("sender") or ""),
        "text_redacted": redacted_text,
        "original_text_sha256": original_text_hash,
        "content_sha256": content_hash,
        "resource_refs_json": refs_json,
        "resource_refs_sha256": refs_hash,
        "redaction_count": redaction_count,
        "resource_ref_count": len(original_refs),
    }


def enqueue_semantic(
    connection: sqlite3.Connection,
    message_id: str,
    content_sha256: str,
    change_kind: str,
    queued_at: str,
) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO semantic_queue(
          message_id,content_sha256,change_kind,queued_at,state
        ) VALUES(?,?,?,?,'pending')
        """,
        (message_id, content_sha256, change_kind, queued_at),
    )
    return cursor.rowcount == 1


def upsert_message(
    connection: sqlite3.Connection,
    message: dict[str, Any],
    observed_at: str,
    run_id: str,
    enqueue_changes: bool = True,
) -> tuple[int, int, Optional[str]]:
    import video_hold
    for index,ref in enumerate(json.loads(message.get('resource_refs_json') or '[]')):
        if not isinstance(ref,dict):continue
        name=str(ref.get('name') or ref.get('fileName') or ref.get('filename') or '')
        kind=video_hold.classify(name,ref.get('mimeType') or ref.get('contentType',''),ref.get('type') or ref.get('resourceType',''))
        if kind:
            video_hold.register('dingtalk_chat',{'message_id':message['message_id'],'resource_index':index},
                name=name or '消息媒体',size=ref.get('size'),version=message['resource_refs_sha256'],kind=kind)
    existing = connection.execute(
        "SELECT content_sha256,current_visible FROM messages WHERE message_id = ?", (message["message_id"],)
    ).fetchone()
    inserted = 1 if existing is None else 0
    updated = 1 if existing is not None and existing["content_sha256"] != message["content_sha256"] else 0
    visible_again = bool(existing is not None and int(existing["current_visible"]) == 0)
    connection.execute(
        """
        INSERT INTO messages(
          message_id,conversation_id,create_time,sender_id,sender_name,text_redacted,
          original_text_sha256,content_sha256,resource_refs_json,resource_refs_sha256,
          redaction_count,first_seen_at,last_seen_at,last_run_id,current_visible
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
        ON CONFLICT(message_id) DO UPDATE SET
          conversation_id=excluded.conversation_id,
          create_time=excluded.create_time,
          sender_id=excluded.sender_id,
          sender_name=excluded.sender_name,
          text_redacted=excluded.text_redacted,
          original_text_sha256=excluded.original_text_sha256,
          content_sha256=excluded.content_sha256,
          resource_refs_json=excluded.resource_refs_json,
          resource_refs_sha256=excluded.resource_refs_sha256,
          redaction_count=excluded.redaction_count,
          last_seen_at=excluded.last_seen_at,
          last_run_id=excluded.last_run_id,
          current_visible=1
        """,
        (
            message["message_id"], message["conversation_id"], message["create_time"],
            message["sender_id"], message["sender_name"], message["text_redacted"],
            message["original_text_sha256"], message["content_sha256"],
            message["resource_refs_json"], message["resource_refs_sha256"],
            message["redaction_count"], observed_at, observed_at, run_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO message_versions(
          message_id,content_sha256,observed_at,text_redacted,original_text_sha256,
          resource_refs_json,resource_refs_sha256,redaction_count
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(message_id,content_sha256) DO UPDATE SET
          text_redacted=excluded.text_redacted,
          original_text_sha256=excluded.original_text_sha256,
          resource_refs_json=excluded.resource_refs_json,
          resource_refs_sha256=excluded.resource_refs_sha256,
          redaction_count=excluded.redaction_count
        """,
        (
            message["message_id"], message["content_sha256"], observed_at,
            message["text_redacted"], message["original_text_sha256"],
            message["resource_refs_json"], message["resource_refs_sha256"],
            message["redaction_count"],
        ),
    )
    change_kind = None
    if inserted:
        change_kind = "insert"
    elif updated:
        change_kind = "update"
    elif visible_again:
        change_kind = "visible_again"
    if enqueue_changes and change_kind:
        connection.execute(
            """
            UPDATE semantic_queue
            SET state='no_business_change',applied_at=?,last_error_code='superseded_before_processing'
            WHERE message_id=? AND content_sha256<>?
              AND state IN ('pending','failed','deferred')
            """,
            (observed_at, str(message["message_id"]), str(message["content_sha256"])),
        )
        enqueue_semantic(
            connection,
            str(message["message_id"]),
            str(message["content_sha256"]),
            change_kind,
            observed_at,
        )
    return inserted, updated, change_kind


def render_ledger(stats: dict[str, Any], database_path: Path, schedule: str) -> str:
    status_label = {
        "success": "已完成",
        "partial": "部分完成，有明确缺口",
        "failed": "失败",
    }.get(str(stats.get("status")), "未知")
    earliest = stats.get("earliest_message_at") or "暂无"
    latest = stats.get("latest_message_at") or "暂无"
    incomplete = int(stats.get("conversations_incomplete") or 0)
    gap_text = (
        f"- 有 {incomplete} 个会话未通过完整性校验；它们保留为失败记录，未按“0 条消息”处理。"
        if incomplete
        else "- 本轮所有可见会话均通过完整性校验。"
    )
    return f"""---
document_type: dingtalk_chat_sync_ledger
schema_version: 2
updated_at: {stats['completed_at']}
source_system: dingtalk
applies_to:
  - project:business-energy
---

## 接入边界

- 只读抓取当前组织账号能看见的钉钉单聊与群聊；不发送、不撤回、不改已读状态、不下载附件。
- 结构化消息正文只保存在本机 `.nosync` 数据库；疑似密码、Token、验证码和私钥内容在落库前脱敏，脱敏消息不保留原文及其可比对指纹。
- Obsidian／iCloud 不保存聊天全文，只保存业务摘要、来源标识、核对状态和本页汇总。
- 单条钉钉消息默认只是来源证据；未经责任人或权威文件交叉核查，不升级为已确认业务事实。

## 当前状态

| 项目 | 结果 |
| --- | --- |
| 本轮状态 | {status_label} |
| 可见会话 | {stats['conversations_discovered']} 个 |
| 完整会话 | {stats['conversations_complete']} 个 |
| 未完整会话 | {stats['conversations_incomplete']} 个 |
| 范围回退完成 | {stats['range_fallbacks']} 个 |
| 本轮返回消息 | {stats['messages_returned']} 条 |
| 本机累计唯一消息 | {stats['unique_messages']} 条 |
| 本轮新增／版本更新 | {stats['messages_inserted']}／{stats['messages_updated']} 条 |
| 脱敏命中 | {stats['redactions']} 处 |
| 资源引用 | {stats['resource_refs']} 个；仅索引，未下载 |
| 可见消息时间范围 | {earliest} ～ {latest} |
| 最近完成时间 | {stats['completed_at']} |
| 自动同步 | {schedule} |

## 完整性缺口

{gap_text}
- `complete`、`hasMore`、分页失败和截断标记均按钉钉真实返回保存；部分结果不得表述为全量完成。

## 本机来源层

- 结构化数据库：`{database_path}`
- 数据库使用 WAL、消息 ID 去重、内容版本链、事件收件箱和语义队列；完整扫描才会把本轮未再出现的消息标为当前不可见，小时增量扫描不得误删历史状态。普通历史版本保留；涉及脱敏内容或敏感资源指纹的旧版本会安全清理。
- 具体业务结论由可信写入器写入独立记忆条目，对外来源只使用 `dingtalk-batch:<UUID>`；真实消息与会话 ID 只留在本机批次映射，不能从本页汇总直接推导事实。
"""


def database_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT COUNT(*) AS unique_messages,
               MIN(create_time) AS earliest_message_at,
               MAX(create_time) AS latest_message_at
        FROM messages
        """
    ).fetchone()
    return dict(row) if row else {"unique_messages": 0, "earliest_message_at": None, "latest_message_at": None}


def parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()


def compute_incremental_window(
    connection: sqlite3.Connection,
    start_override: Optional[str],
    end_override: Optional[str],
    overlap_minutes: int,
    initial_lookback_hours: int,
) -> tuple[str, str]:
    now = datetime.now().astimezone()
    if start_override:
        start = parse_timestamp(start_override)
    else:
        row = connection.execute(
            """
            SELECT window_end FROM sync_runs
            WHERE mode='incremental' AND status='success' AND window_end IS NOT NULL
            ORDER BY completed_at DESC LIMIT 1
            """
        ).fetchone()
        anchor = str(row["window_end"]) if row and row["window_end"] else ""
        if not anchor:
            row = connection.execute(
                """
                SELECT completed_at FROM sync_runs
                WHERE mode='full' AND status='success' AND completed_at IS NOT NULL
                ORDER BY completed_at DESC LIMIT 1
                """
            ).fetchone()
            anchor = str(row["completed_at"]) if row and row["completed_at"] else ""
        start = parse_timestamp(anchor) if anchor else now - timedelta(hours=initial_lookback_hours)
        start -= timedelta(minutes=overlap_minutes)
    end = parse_timestamp(end_override) if end_override else now + timedelta(minutes=1)
    if start >= end:
        raise SyncError("invalid_incremental_window", "incremental start must be earlier than end")
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def recover_stale_runs(connection: sqlite3.Connection, timeout_seconds: int) -> int:
    threshold = datetime.now().astimezone() - timedelta(seconds=max(60, timeout_seconds * 2))
    recovered = 0
    for row in connection.execute("SELECT run_id,started_at FROM sync_runs WHERE status='running'").fetchall():
        try:
            started = parse_timestamp(str(row["started_at"]))
        except (TypeError, ValueError):
            started = datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if started > threshold:
            continue
        stamp = now_iso()
        connection.execute(
            """
            UPDATE sync_runs SET status='interrupted',completed_at=?,error_code='interrupted_after_timeout',
              error_detail='previous run ended without a completion receipt'
            WHERE run_id=? AND status='running'
            """,
            (stamp, str(row["run_id"])),
        )
        recovered += 1
    if recovered:
        connection.commit()
    return recovered


def run_sync(args: argparse.Namespace) -> dict[str, Any]:
    config = json.loads(args.config.read_text(encoding="utf-8"))
    chat_root = Path(args.external_root or config["external_root"]) / "dingtalk-chat"
    database_path = Path(args.database or chat_root / "state/chat.sqlite").resolve()
    try:
        database_lock = checkpoint.acquire_database_lock(database_path)
    except ValueError:
        raise SyncError("already_running", "同一聊天库已有读取任务，保留既有重试预算。")
    try:
        return _run_sync_locked(args)
    finally:
        database_lock.close()


def _run_sync_locked(args: argparse.Namespace) -> dict[str, Any]:
    config = json.loads(args.config.read_text(encoding="utf-8"))
    chat_config = config.get("dingtalk_chat", {})
    if not isinstance(chat_config, dict):
        raise SyncError("invalid_config", "dingtalk_chat config must be an object")
    dws_binary = Path(args.dws_bin or chat_config.get("dws_bin") or _archive_path('/Users/example/.local/bin/dws'))
    if not dws_binary.is_file() or not os.access(dws_binary, os.X_OK):
        raise SyncError("dws_missing", "configured dws binary is missing or not executable")
    external_root = Path(args.external_root or config["external_root"])
    memory_root = Path(args.memory_root or config["memory_root"])
    chat_root = external_root / "dingtalk-chat"
    database_path = Path(args.database or chat_root / "state" / "chat.sqlite")
    mode = str(args.mode or "full")
    status_path = chat_root / "state" / ("status.json" if mode == "full" else "incremental_status.json")
    log_path = chat_root / "logs" / "sync.jsonl"
    if mode != "incremental" and chat_read_recovery.cooldown_active(status_path):
        raise SyncError("chat_read_cooldown", "等待来源明确要求的冷却时间，保留原失败与下次重试时间。")
    ledger_path = memory_root / "99_规则与模板" / "钉钉聊天同步台账.md"
    timeout = int(chat_config.get("timeout_seconds", 180))
    page_size = int(chat_config.get("page_size", 100))
    page_limit = int(chat_config.get("page_limit", 500))
    max_items = int(chat_config.get("max_items_per_conversation", 50000))
    max_text_chars = int(chat_config.get("max_text_chars", 200000))
    range_fallback_start = str(chat_config.get("range_fallback_start", "2000-01-01T00:00:00+08:00"))
    schedule = str(chat_config.get("schedule_label", "每日 23:45（Asia/Shanghai）"))
    overlap_minutes = int(chat_config.get("incremental_overlap_minutes", 10))
    initial_lookback_hours = int(chat_config.get("incremental_initial_lookback_hours", 2))
    run_id = str(uuid.uuid4())
    started_at = now_iso()
    connection = connect_database(database_path)
    stale_runs_recovered = recover_stale_runs(connection, timeout)
    window_start = None
    window_end = None
    window = None
    probe = False
    reconciliation = None
    if mode == "incremental":
        checkpoint.ensure_schema(connection)
        window = checkpoint.active(connection)
        if window and ((args.start and parse_timestamp(args.start) != parse_timestamp(window["window_start"]))
                       or (args.end and parse_timestamp(args.end) != parse_timestamp(window["window_end"]))):
            connection.close()
            raise SyncError("chat_checkpoint_range_conflict", "已有未完成固定补查范围，不能改写或越过其水位。")
        if window is None:
            window_start, window_end = compute_incremental_window(
                connection, args.start, args.end, overlap_minutes, initial_lookback_hours)
            window = checkpoint.new_window(connection, window_start, window_end, started_at)
        window_start, window_end = window["window_start"], window["window_end"]
        if not checkpoint.due(window, started_at):
            reconciliation = checkpoint.progress(connection, window["window_id"])
            receipt = {"schema_version": SCHEMA_VERSION, "mode": mode, "run_id": window.get("last_run_id"),
                       "status": "partial", "started_at": started_at, "completed_at": started_at,
                       "window_start": window_start, "window_end": window_end,
                       "conversations_discovered": reconciliation["conversations_discovered"],
                       "conversations_complete": reconciliation["conversations_complete"],
                       "conversations_incomplete": reconciliation["conversations_incomplete"],
                       "messages_inserted": 0, "messages_updated": 0, "messages_returned": 0,
                       "read_attempted": False, "read_recovery": checkpoint.recovery_receipt(reconciliation, waiting=True),
                       "reconciliation": reconciliation}
            connection.close()
            return receipt
        # Preserve upstream cooldown from the previous implementation on migration.
        if not window["attempt_count"] and chat_read_recovery.cooldown_active(status_path):
            prior = json.loads(status_path.read_text()).get("read_recovery", {})
            with connection:
                connection.execute("UPDATE chat_reconcile_windows SET status='retry_wait',next_due=?,error_code='chat_read_cooldown' WHERE window_id=?",
                                   (prior["next_retry_at"],window["window_id"]))
            connection.close()
            raise SyncError("chat_read_cooldown", "保留来源冷却时间，尚未发起读取。")
        probe = checkpoint.begin(connection, window, run_id, started_at)
    connection.execute(
        """
        INSERT INTO sync_runs(run_id,started_at,status,mode,window_start,window_end)
        VALUES(?,?,'running',?,?,?)
        """,
        (run_id, started_at, mode, window_start, window_end),
    )
    connection.commit()
    append_log(log_path, "sync_started", run_id=run_id, mode=mode)

    read_failures = []
    committed_receipt = None
    totals = {
        "conversations_discovered": 0,
        "conversations_complete": 0,
        "conversations_incomplete": 0,
        "range_fallbacks": 0,
        "messages_returned": 0,
        "messages_inserted": 0,
        "messages_updated": 0,
        "messages_hidden": 0,
        "redactions": 0,
        "resource_refs": 0,
    }
    try:
        if window and window["list_complete"]:
            conversations = checkpoint.targets(connection, window["window_id"], probe)
        else:
            list_started = time.monotonic()
            try:
                conversation_payload, returncode, error_code, error_detail = run_dws(
                    dws_binary, ["chat", "+conversation-list", "--page-all"], timeout)
            except SyncError as exc:
                conversation_payload, returncode, error_code, error_detail = {}, 1, exc.code, exc.message
            conversations = conversation_payload.get("conversations")
            conversation_complete = (
                returncode == 0
                and conversation_payload.get("complete") is True
                and conversation_payload.get("partial") is not True
                and conversation_payload.get("hasMore") is not True
                and conversation_payload.get("truncated") is not True
                and not conversation_payload.get("failures")
                and int(conversation_payload.get("failedCount") or 0) == 0
                and isinstance(conversations, list)
            )
            if not conversation_complete:
                evidence = chat_read_recovery.failure_evidence(conversation_payload, error_code, error_detail)
                evidence.update(phase="conversation_list", elapsed_ms=int((time.monotonic()-list_started)*1000))
                append_log(log_path, "chat_read_failure", run_id=run_id, **evidence)
                read_failures.append(evidence)
                raise SyncError(evidence["error_code"], "会话列表读取未完整；认证与不可重试错误不会自动放行。")
            listed_ids = {
                str(item.get("openConversationId") or "").strip()
                for item in conversations
                if isinstance(item, dict)
            }
            event_only = connection.execute(
                """
                SELECT conversation_id,conversation_name FROM conversations
                WHERE last_complete_scan_at IS NULL OR last_scan_status='event'
                ORDER BY first_seen_at
                """
            ).fetchall()
            for known in event_only:
                conversation_id = str(known["conversation_id"] or "").strip()
                if conversation_id and conversation_id not in listed_ids:
                    conversations.append(
                        {
                            "openConversationId": conversation_id,
                            "conversationName": str(known["conversation_name"] or ""),
                            "discoveredFrom": "local_event_history",
                        }
                    )
                    listed_ids.add(conversation_id)
            if window:
                try:
                    checkpoint.set_members(connection, window["window_id"], conversations, now_iso(), run_id, range_fallback_start)
                except ValueError:
                    raise SyncError("chat_read_blocked", "会话列表结构异常，保留补查范围并停止自动读取。")
                conversations = checkpoint.targets(connection, window["window_id"], probe)
        totals["conversations_discovered"] = len(conversations)
        if window:
            existing = checkpoint.progress(connection, window["window_id"])
            totals.update({key:existing[key] for key in ("conversations_discovered", "conversations_complete", "conversations_incomplete")})

        for raw_conversation in conversations:
            if not isinstance(raw_conversation, dict):
                totals["conversations_incomplete"] += 1
                continue
            conversation_id = str(raw_conversation.get("openConversationId") or "").strip()
            conversation_name = str(raw_conversation.get("conversationName") or "").strip()
            if not conversation_id:
                totals["conversations_incomplete"] += 1
                continue
            scan_started = now_iso()
            historical_backfill = mode == "incremental" and (
                raw_conversation.get("historicalBackfill") is True
                or raw_conversation.get("discoveredFrom") == "local_event_history"
            )
            conversation_scan_mode = "historical_backfill" if historical_backfill else mode
            if mode == "full":
                message_arguments = [
                    "chat", "+chat-messages", "--group", conversation_id,
                    "--direction", "older", "--page-all",
                    "--limit", str(page_size), "--page-limit", str(page_limit),
                    "--max-items", str(max_items),
                ]
            else:
                message_arguments = [
                    "chat", "+chat-messages", "--group", conversation_id,
                    "--start", str(raw_conversation.get("range_start") or window_start), "--end", str(raw_conversation.get("range_end") or window_end),
                    "--order", "asc", "--page-all",
                    "--limit", str(page_size), "--page-limit", str(page_limit),
                    "--max-items", str(max_items),
                ]
            message_arguments += ["--no-reactions", "--page-delay", "200"]
            payload, returncode, error_code, error_detail = read_messages_with_recovery(
                dws_binary, message_arguments, timeout, log_path, run_id, conversation_id,
            )
            raw_messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
            if not window and should_try_range_fallback(mode, historical_backfill, payload, returncode):
                fallback_end = (datetime.now().astimezone() + timedelta(days=1)).isoformat(timespec="seconds")
                fallback_payload, fallback_returncode, fallback_error_code, fallback_error_detail = run_dws(
                    dws_binary,
                    [
                        "chat", "+chat-messages", "--group", conversation_id,
                        "--start", range_fallback_start, "--end", fallback_end,
                        "--order", "asc", "--page-all",
                        "--limit", str(page_size), "--page-limit", str(page_limit),
                        "--max-items", str(max_items),
                    ],
                    timeout,
                )
                if message_payload_complete(fallback_payload, fallback_returncode):
                    payload = dict(fallback_payload)
                    payload["stopReason"] = f"range_asc_fallback:{fallback_payload.get('stopReason') or 'source_complete'}"
                    returncode = fallback_returncode
                    error_code = fallback_error_code
                    error_detail = fallback_error_detail
                    raw_messages = payload.get("messages", [])
                    totals["range_fallbacks"] += 1
            normalized_messages: list[dict[str, Any]] = []
            normalization_failures = 0
            ordered_raw_messages = sorted(
                raw_messages,
                key=lambda item: str(item.get("createTime") or "") if isinstance(item, dict) else "",
            )
            previous_text = ""
            if mode == "incremental" and ordered_raw_messages:
                first_time = str(ordered_raw_messages[0].get("createTime") or "") if isinstance(ordered_raw_messages[0], dict) else ""
                if first_time:
                    prior = connection.execute(
                        """
                        SELECT text_redacted FROM messages
                        WHERE conversation_id=? AND create_time<? AND current_visible=1
                        ORDER BY create_time DESC LIMIT 1
                        """,
                        (conversation_id, first_time),
                    ).fetchone()
                    previous_text = str(prior["text_redacted"] or "") if prior else ""
            for raw_message in ordered_raw_messages:
                try:
                    current_text = str(raw_message.get("text") or "") if isinstance(raw_message, dict) else ""
                    force_sensitive_reply = bool(
                        previous_text
                        and SENSITIVE_PROMPT_RE.search(previous_text)
                        and 0 < len(current_text.strip()) <= 200
                    )
                    normalized_messages.append(
                        normalize_message(raw_message, conversation_id, max_text_chars, force_sensitive_reply)
                    )
                    previous_text = current_text
                except (TypeError, ValueError):
                    normalization_failures += 1

            scan_complete = (
                message_payload_complete(payload, returncode)
                and returncode == 0
                and payload.get("complete") is True
                and payload.get("partial") is not True
                and payload.get("hasMore") is not True
                and payload.get("truncated") is not True
                and int(payload.get("failedCount") or 0) == 0
                and normalization_failures == 0
            )
            scan_status = "complete" if scan_complete else "incomplete"
            scan_completed = now_iso()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO conversations(
                  conversation_id,conversation_name,first_seen_at,last_seen_at,last_scan_at,
                  last_complete_scan_at,last_scan_status,last_error_code,last_error_detail,
                  last_message_at,visible_message_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(conversation_id) DO UPDATE SET
                  conversation_name=excluded.conversation_name,
                  last_seen_at=excluded.last_seen_at,
                  last_scan_at=excluded.last_scan_at,
                  last_complete_scan_at=COALESCE(excluded.last_complete_scan_at,conversations.last_complete_scan_at),
                  last_scan_status=excluded.last_scan_status,
                  last_error_code=excluded.last_error_code,
                  last_error_detail=excluded.last_error_detail
                """,
                (
                    conversation_id, conversation_name, scan_started, scan_completed, scan_completed,
                    scan_completed if scan_complete and (mode == "full" or historical_backfill) else None, scan_status,
                    None if scan_complete else error_code,
                    None if scan_complete else error_detail,
                    None,
                ),
            )
            for message in normalized_messages:
                inserted, updated, _change_kind = upsert_message(connection, message, scan_completed, run_id)
                if int(message["redaction_count"]) > 0:
                    connection.execute(
                        "DELETE FROM message_versions WHERE message_id=? AND content_sha256<>?",
                        (message["message_id"], message["content_sha256"]),
                    )
                totals["messages_inserted"] += inserted
                totals["messages_updated"] += updated
                totals["redactions"] += int(message["redaction_count"])
                totals["resource_refs"] += int(message["resource_ref_count"])
            # A bounded incremental/backfill window cannot prove historical deletions.
            if scan_complete and mode == "full":
                hidden_rows = connection.execute(
                    """
                    SELECT message_id,content_sha256 FROM messages
                    WHERE conversation_id=? AND current_visible=1 AND last_run_id<>?
                    """,
                    (conversation_id, run_id),
                ).fetchall()
                for hidden in hidden_rows:
                    enqueue_semantic(
                        connection,
                        str(hidden["message_id"]),
                        str(hidden["content_sha256"]),
                        "hidden",
                        scan_completed,
                    )
                connection.execute(
                    "UPDATE messages SET current_visible=0 WHERE conversation_id=? AND last_run_id<>?",
                    (conversation_id, run_id),
                )
                totals["messages_hidden"] += len(hidden_rows)
            visible = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND current_visible=1",
                (conversation_id,),
            ).fetchone()[0]
            latest = connection.execute(
                "SELECT MAX(create_time) FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE conversations SET visible_message_count=?,last_message_at=? WHERE conversation_id=?",
                (visible, latest, conversation_id),
            )
            connection.execute(
                """
                INSERT INTO conversation_scans(
                  run_id,conversation_id,started_at,completed_at,status,returned_count,
                  complete,partial,has_more,pages_fetched,failed_count,truncated,
                  stop_reason,error_code,error_detail,normalization_failures,scan_mode
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, conversation_id, scan_started, scan_completed, scan_status,
                    len(raw_messages), as_bool(payload.get("complete")), as_bool(payload.get("partial")),
                    as_bool(payload.get("hasMore")), int(payload.get("pagesFetched") or 0),
                    int(payload.get("failedCount") or 0), as_bool(payload.get("truncated")),
                    str(payload.get("stopReason") or ""),
                    None if scan_complete else error_code,
                    None if scan_complete else error_detail,
                    normalization_failures,
                    conversation_scan_mode,
                ),
            )
            evidence = ({"error_code": "chat_read_blocked", "retryable": False, "phase": "message_structure"}
                        if normalization_failures else payload.get("read_recovery") or
                        chat_read_recovery.failure_evidence(payload, error_code, error_detail)) if not scan_complete else {}
            partitioned = False
            if window:
                if (not scan_complete and not normalization_failures and returncode == 0
                        and not payload.get("error") and not payload.get("failures")
                        and int(payload.get("failedCount") or 0) == 0
                        and evidence.get("error_code") not in checkpoint.HARD_ERRORS
                        and evidence.get("source_code") in {None, "unclassified"}
                        and evidence.get("retryable") is not False
                        and int(evidence.get("retry_after_seconds") or 0) == 0
                        and (payload.get("truncated") is True
                             or payload.get("truncatedByPageLimit") is True
                             or payload.get("truncatedByResultLimit") is True)):
                    evidence = {"error_code":"chat_reconcile_incomplete", "source_code":"pagination_limit", "phase":"messages"}
                outcome = checkpoint.record_target(connection, window["window_id"], conversation_id, run_id,
                    scan_completed, scan_complete, evidence, raw_conversation["range_start"], raw_conversation["range_end"])
                partitioned, evidence = outcome["partitioned"], outcome["evidence"]
            connection.commit()
            totals["messages_returned"] += len(raw_messages)
            if scan_complete:
                totals["conversations_complete"] += 1
            else:
                totals["conversations_incomplete"] += 1
                if partitioned:
                    continue
                read_failures.append(evidence)
                if (read_failures[-1]["error_code"] in {"chat_read_auth_required", "chat_read_blocked"}
                        or int(evidence.get("retry_after_seconds") or 0) > 0):
                    # Do not repeat an identity/permission failure against other conversations.
                    totals["conversations_incomplete"] = totals["conversations_discovered"] - totals["conversations_complete"]
                    break

        completed_at = now_iso()
        if window:
            reconciliation = checkpoint.finish(connection, window["window_id"], run_id, completed_at, read_failures, probe, commit=False)
            totals.update({key:reconciliation[key] for key in ("conversations_discovered", "conversations_complete", "conversations_incomplete")})
        final_status = "success" if (reconciliation["status"] == "complete" if reconciliation else totals["conversations_incomplete"] == 0) else "partial"
        connection.execute(
            """
            UPDATE sync_runs SET completed_at=?,status=?,conversations_discovered=?,
              conversations_complete=?,conversations_incomplete=?,messages_returned=?,
              messages_inserted=?,messages_updated=?,messages_hidden=?,redactions=?,resource_refs=?
            WHERE run_id=?
            """,
            (
                completed_at, final_status, totals["conversations_discovered"],
                totals["conversations_complete"], totals["conversations_incomplete"],
                totals["messages_returned"], totals["messages_inserted"],
                totals["messages_updated"], totals["messages_hidden"], totals["redactions"],
                totals["resource_refs"], run_id,
            ),
        )
        connection.commit()
        # Authoritative completion and its source watermark were committed together.
        committed_receipt = {"schema_version": SCHEMA_VERSION, "run_id":run_id,"mode":mode,
                             "window_start":window_start,"window_end":window_end,
                             "started_at":started_at,"completed_at":completed_at,"status":final_status,
                             **totals,"reconciliation":reconciliation,"read_attempted":True,
                             "read_recovery":checkpoint.recovery_receipt(reconciliation) if reconciliation and final_status!="success" else chat_read_recovery.summarize_failures(read_failures)}
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        stats = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "mode": mode,
            "window_start": window_start,
            "window_end": window_end,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": final_status,
            "stale_runs_recovered": stale_runs_recovered,
            **totals,
            "read_recovery": checkpoint.recovery_receipt(reconciliation) if reconciliation and final_status != "success" else chat_read_recovery.summarize_failures(read_failures),
            "read_attempted": True,
            "reconciliation": reconciliation,
            **database_stats(connection),
            "database_user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "database_quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "database_secure_delete": connection.execute("PRAGMA secure_delete").fetchone()[0],
        }
        atomic_write(status_path, json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if not args.no_ledger and mode == "full":
            atomic_write(ledger_path, render_ledger(stats, database_path, schedule), mode=0o644)
        append_log(
            log_path,
            "sync_completed",
            run_id=run_id,
            mode=mode,
            status=final_status,
            conversations_discovered=totals["conversations_discovered"],
            conversations_complete=totals["conversations_complete"],
            conversations_incomplete=totals["conversations_incomplete"],
            messages_returned=totals["messages_returned"],
            messages_inserted=totals["messages_inserted"],
            messages_updated=totals["messages_updated"],
            messages_hidden=totals["messages_hidden"],
            range_fallbacks=totals["range_fallbacks"],
        )
        return stats
    except Exception as exc:
        connection.rollback()
        if committed_receipt is not None:
            # Export/log errors cannot rewrite a committed source result as failed.
            try:
                append_log(log_path, "receipt_export_failed", run_id=run_id, error_code=type(exc).__name__)
            except OSError:
                pass
            return {**committed_receipt, "receipt_export_error": type(exc).__name__}
        completed_at = now_iso()
        code = exc.code if isinstance(exc, SyncError) else type(exc).__name__
        detail = exc.message if isinstance(exc, SyncError) else "unexpected sync failure"
        if window:
            if not read_failures:
                evidence = chat_read_recovery.failure_evidence({}, code, detail)
                if isinstance(exc, (sqlite3.OperationalError, OSError)):
                    evidence = {"error_code": "chat_read_transient", "retryable": True, "phase": "local_checkpoint_io"}
                elif not isinstance(exc, SyncError):
                    evidence = {"error_code": "chat_checkpoint_invalid", "retryable": False}
                read_failures.append(evidence)
            reconciliation = checkpoint.finish(connection, window["window_id"], run_id, completed_at, read_failures, probe,
                                               fatal=read_failures[-1]["error_code"])
        connection.execute(
            "UPDATE sync_runs SET completed_at=?,status='failed',error_code=?,error_detail=? WHERE run_id=?",
            (completed_at, code, detail[:300], run_id),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        failure = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "mode": mode,
            "window_start": window_start,
            "window_end": window_end,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": "failed",
            "error_code": code,
            "error_detail": detail[:300],
            "read_recovery": checkpoint.recovery_receipt(reconciliation) if reconciliation else chat_read_recovery.summarize_failures(read_failures),
            "reconciliation": reconciliation,
            **totals,
            **database_stats(connection),
        }
        atomic_write(status_path, json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if not args.no_ledger and mode == "full":
            atomic_write(ledger_path, render_ledger(failure, database_path, schedule), mode=0o644)
        append_log(log_path, "sync_failed", run_id=run_id, mode=mode, error_code=code)
        if isinstance(exc, SyncError):
            exc.receipt = failure
        raise
    finally:
        connection.close()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read DingTalk chat history into the private BUSINESS source database")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--dws-bin")
    parser.add_argument("--external-root")
    parser.add_argument("--memory-root")
    parser.add_argument("--database")
    parser.add_argument("--mode", choices=("full", "incremental"), default="full")
    parser.add_argument("--start", help="incremental range start; ISO-8601 or local datetime")
    parser.add_argument("--end", help="incremental range end; ISO-8601 or local datetime")
    parser.add_argument("--no-ledger", action="store_true")
    return parser.parse_args()


def main() -> int:
    lock_handle = None
    try:
        lock_handle = acquire_lock()
        args=parse_arguments()
        stats = run_sync(args)
        import media_classification
        media_classification.spawn(args.config)
        print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
        return 0 if stats.get("status") == "success" else 2
    except SyncError as exc:
        print(json.dumps(getattr(exc, "receipt", None) or {"status": "failed", "error_code": exc.code, "error_detail": exc.message}, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_code": type(exc).__name__}, ensure_ascii=False))
        return 1
    finally:
        if lock_handle is not None:
            lock_handle.close()


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    sys.exit(main())
