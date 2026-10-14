#!/usr/bin/env python3
"""Governed enterprise-mail mirror, business-memory writer, and Feishu reporter.

Raw mail, identifiers, bodies, attachments, and model intermediates remain in
local .nosync storage. Obsidian receives only source indexes and governed
business-memory candidates. Feishu receives only a three-section redacted
batch report and a delivery receipt hash.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import email
import fcntl
import hashlib
import html
import json
import os
import plistlib
import re
import shutil
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from email import policy
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Optional


os.umask(0o077)

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = BRIDGE_ROOT / "config.json"
MAINTENANCE_PATH = BRIDGE_ROOT / "state" / "maintenance-mode.json"
RUN_LOCK = BRIDGE_ROOT / "state" / "email-sync.lock"
EMAIL_PROMPT = BRIDGE_ROOT / "prompts" / "email_memory_proposal.md"
EMAIL_SCHEMA = BRIDGE_ROOT / "schemas" / "email_memory_proposal.schema.json"
HERMES_SCRIPTS = Path(_archive_path('/Users/example/.hermes/profiles/feishubot/scripts'))
sys.path.insert(0, str(BRIDGE_ROOT / "bin"))
sys.path.insert(0, str(HERMES_SCRIPTS))

import dingtalk_memory_automation as memory  # noqa: E402
import business_page_router as business_router  # noqa: E402
import claim_reconciliation as reconciliation  # noqa: E402
import dingtalk_minutes_review_notice as feishu_transport  # noqa: E402
import dingtalk_sync_v2 as syncv2  # noqa: E402
import win11_mail_attachments as win11_attachments  # noqa: E402
import video_hold
import email_batched_input  # noqa: E402
import mail_auto_recovery
import mail_semantic_recovery
import mail_transaction_recovery
import mail_legacy_recovery


CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
LOCAL_PATH_RE = re.compile(
    r"(?:/(?:Users|Volumes|private|tmp)/[^\s]+|[A-Za-z]:\\[^\s]+|\\\\[^\s\\]+\\[^\s]+)"
)
SECRET_RE = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,}\b|\bAKIA[0-9A-Z]{16}\b|"
    r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|"
    r"(?:password|passwd|token|secret|验证码|密码)\s*[:=]\s*\S{6,}",
    re.IGNORECASE,
)
PUBLIC_SENSITIVE_RE = re.compile(
    r"(?i)(?:[$€¥￥]\s*[0-9][0-9,]*(?:\.[0-9]+)?|"
    r"(?:usd|eur|cny|rmb)\s*[0-9][0-9,]*(?:\.[0-9]+)?|"
    r"[0-9][0-9,.]*\s*(?:kwh|mwh|gwh|kw|mw|gw|v|a|mm|cm|kg|usd|eur|cny|rmb|万元|美元|欧元|人民币|天|周)|"
    r"\b[0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2}\b|"
    r"\b[0-9]+(?:\.[0-9]+)?%|"
    r"\b\d{6,}\b)"
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MEMORY_ID_RE = re.compile(r"^BUSINESS-[0-9]{8}-[0-9]{3}$")
TARGET_ID_RE = re.compile(r"oc_[A-Za-z0-9_-]{16,}")
PUBLIC_INTERNAL_ID_RE = re.compile(
    r"\bBUSINESS-[0-9]{8}-[0-9]{3}\b|\bS[0-9]{3}\b|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
TEST_SUBJECT_MARKER = "BUSINESS-Mail-Mirror"
BODY_LENGTH_VALIDATOR_VERSION = "body-length-v2"
TRANSIENT_ARCHIVE_RETRY_DELAYS = (60, 300, 900, 3600)
ARCHIVE_PERSISTENT_PROBE_SECONDS = 6 * 3600

HAZARDOUS_EXTENSIONS = {
    ".apk", ".app", ".applescript", ".bat", ".bin", ".cjs", ".cmd", ".com",
    ".command", ".cpl", ".dll", ".dmg", ".exe", ".hta", ".iso", ".jar", ".js",
    ".jse", ".lnk", ".mjs", ".msi", ".pkg", ".pl", ".ps1", ".py", ".pyw",
    ".rb", ".reg", ".scpt", ".scr", ".sh", ".so", ".vb", ".vbs", ".vbe",
    ".workflow", ".wsf", ".chm", ".doc", ".dot", ".docm", ".dotm",
    ".pps", ".ppt", ".ppam", ".pptm", ".ppsm", ".sldm",
    ".xla", ".xlam", ".xls", ".xlsb", ".xlsm", ".xlt", ".xltm", ".xll",
}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log"}
OFFICE_XML_EXTENSIONS = {".docx", ".xlsx", ".pptx"}


class EmailSyncError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"script", "style", "iframe", "object"}:
            self.skip_depth += 1
        elif tag.lower() in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "iframe", "object"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag.lower() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


import email_phases


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def normalize_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def body_length_evidence(
    message: dict[str, Any],
    markdown_body: str,
    *,
    complete_eml_export: bool = False,
) -> dict[str, Any]:
    """Validate completeness using the UTF-16 unit used by the DWS field.

    Python ``len`` counts Unicode code points while the upstream field follows
    JavaScript/UTF-16 string length. Supplementary characters therefore use
    two upstream units. EML size remains independent evidence and never
    changes this exact comparison.
    """
    del complete_eml_export  # Kept for source compatibility with older callers.
    if "bodyHasMore" not in message:
        raise EmailSyncError("body_completeness_missing", "mail body completeness flag is missing")
    if normalize_bool(message.get("bodyHasMore")):
        raise EmailSyncError("body_truncated", "mail body is incomplete")
    if "bodyTotalLength" not in message:
        raise EmailSyncError("body_length_missing", "mail body length is missing")
    try:
        declared = int(message.get("bodyTotalLength"))
    except (TypeError, ValueError) as exc:
        raise EmailSyncError("body_length_invalid", "mail body length is invalid") from exc
    if declared < 0:
        raise EmailSyncError("body_length_invalid", "mail body length is invalid")
    codepoints = len(markdown_body)
    utf16_units = len(markdown_body.encode("utf-16-le", errors="surrogatepass")) // 2
    utf16_delta = declared - utf16_units
    if utf16_delta != 0:
        raise EmailSyncError("body_length_mismatch", "mail body length is incomplete")
    return {
        "body_total_length": declared,
        # Preserve the legacy field's code-point meaning for old readers.
        "body_observed_length": codepoints,
        "body_length_delta": declared - codepoints,
        "body_observed_codepoints": codepoints,
        "body_observed_utf16_units": utf16_units,
        "body_length_unit": "utf16_code_units",
        "body_length_delta_utf16": utf16_delta,
        "body_length_validator_version": BODY_LENGTH_VALIDATOR_VERSION,
        "body_length_status": "utf16_exact",
    }


def ensure_private_dir(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise EmailSyncError("unsafe_directory", "governed directory is not a regular directory")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise EmailSyncError("unsafe_directory", "governed directory became unsafe")
    path.chmod(0o700)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def safe_filename(value: str, limit: int = 90) -> str:
    cleaned = CONTROL_RE.sub("", str(value or ""))
    cleaned = re.sub(r"[\x00/\\:*?\"<>|\r\n\[\]#^]+", "-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return (cleaned or "attachment")[:limit]


def html_to_text(value: str) -> str:
    parser = TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html.unescape(value))


def redact_for_model(value: Any, limit: int) -> str:
    text = CONTROL_RE.sub("", value if isinstance(value, str) else "")
    text = SECRET_RE.sub("[敏感凭据已省略]", text)
    text = EMAIL_RE.sub("[邮箱已省略]", text)
    text = MOBILE_RE.sub("[手机号已省略]", text)
    text = URL_RE.sub("[链接已省略]", text)
    text = LOCAL_PATH_RE.sub("[本地路径已省略]", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    return text[:limit]


def public_report_text(value: Any, limit: int = 220) -> str:
    text = redact_for_model(value, limit * 2)
    text = PUBLIC_SENSITIVE_RE.sub("[敏感业务数据已省略]", text)
    text = re.sub(r"</?at\b[^>]*>", "[提及已省略]", text, flags=re.IGNORECASE)
    text = re.sub(r"[\r\n]+", " ", text)
    text = text.replace("@", "＠").replace("<", "＜").replace(">", "＞")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def contains_private_value(value: Any) -> bool:
    text = str(value or "")
    return bool(
        EMAIL_RE.search(text)
        or MOBILE_RE.search(text)
        or SECRET_RE.search(text)
        or LOCAL_PATH_RE.search(text)
        or TARGET_ID_RE.search(text)
        or PUBLIC_INTERNAL_ID_RE.search(text)
    )


def parse_json_stream(output: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []
    position = 0
    while position < len(output):
        while position < len(output) and output[position] not in "[{":
            position += 1
        if position >= len(output):
            break
        try:
            value, end = decoder.raw_decode(output, position)
        except json.JSONDecodeError:
            position += 1
            continue
        values.append(value)
        position = end
    return values


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmailSyncError("invalid_config", "bridge config is unreadable") from exc
    section = config.get("email_sync")
    if not isinstance(section, dict):
        raise EmailSyncError("invalid_config", "email_sync config is missing")
    fixed = {
        "mailbox": 'fixture_9413a2bc@example.invalid',
        "mailbox_type": "ORG",
        "baseline_folders": ["2", "1"],
        "incremental_folder": "2",
        "excluded_folders": ["3", "5", "6"],
        "poll_minutes": 30,
        "overlap_hours": 48,
        "daily_rescan_hours": 24,
        "expected_group_name": "邮件同步",
        "group_alias": "邮件同步",
        "subscription_only": True,
        "allow_paid_api_fallback": False,
        "allow_model_fallback": False,
        "writer_label": "com.example.business-enterprise-email-sync",
    }
    for key, expected in fixed.items():
        if section.get(key) != expected:
            raise EmailSyncError("invalid_config", f"governed email setting is invalid: {key}")
    if not isinstance(section.get("enabled"), bool) or not isinstance(section.get("writer_registered"), bool):
        raise EmailSyncError("invalid_config", "email enablement flags must be explicit booleans")
    preapplied_keys = section.get("preapplied_message_keys")
    if (
        not isinstance(preapplied_keys, list)
        or len(preapplied_keys) != len(set(preapplied_keys))
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None
            for value in preapplied_keys
        )
    ):
        raise EmailSyncError("invalid_config", "pre-applied email memory keys are invalid")
    required_strings = (
        "state_root", "archive_root", "read_safety_receipt", "target_map_path",
        "feishu_profile_root", "dws_bin", "codex_bin", "model", "reasoning_effort", "writer_plist_path",
    )
    if any(not isinstance(section.get(key), str) or not str(section.get(key)).strip() for key in required_strings):
        raise EmailSyncError("invalid_config", "email paths and model settings must be explicit")
    bounded = {
        "max_page_size": (1, 100),
        "max_message_chars": (1000, 50000),
        "max_attachment_text_chars": (1000, 50000),
        "max_conversation_chars": (5000, 120000),
        "max_model_calls_per_run": (1, 100),
        "dws_timeout_seconds": (15, 300),
        "model_timeout_seconds": (60, 3600),
    }
    for key, (minimum, maximum) in bounded.items():
        value = normalize_int(section.get(key))
        if not minimum <= value <= maximum:
            raise EmailSyncError("invalid_config", f"email setting out of range: {key}")
    external_root = Path(str(config.get("external_root") or "")).resolve()
    expected_state = external_root / "email-sync"
    expected_archive = external_root / "email-archive" / "automatic"
    if Path(str(section["state_root"])).resolve() != expected_state:
        raise EmailSyncError("invalid_config", "email state root is outside the governed location")
    if Path(str(section["archive_root"])).resolve() != expected_archive:
        raise EmailSyncError("invalid_config", "email archive root is outside the governed location")
    receipt = Path(str(section["read_safety_receipt"])).resolve()
    try:
        receipt.relative_to(expected_state)
    except ValueError as exc:
        raise EmailSyncError("invalid_config", "read-safety receipt is outside email state") from exc
    profile_root = Path(str(section["feishu_profile_root"])).resolve()
    if profile_root != Path(str(feishu_transport.PROFILE_ROOT)).resolve():
        raise EmailSyncError("invalid_config", "email target profile does not match the active Feishu gateway")
    target_map = Path(str(section["target_map_path"])).resolve()
    try:
        target_map.relative_to(profile_root / "cron")
    except ValueError as exc:
        raise EmailSyncError("invalid_config", "email target map is outside the Feishu profile") from exc
    if section.get("writer_registered") is True:
        expected_plist = Path(str(section["writer_plist_path"])).resolve()
        writers = config.get("memory_writers")
        matches = [
            writer for writer in writers or []
            if isinstance(writer, dict) and writer.get("label") == section["writer_label"]
        ]
        if len(matches) != 1:
            raise EmailSyncError("invalid_config", "email writer is not uniquely registered with maintenance")
        writer = matches[0]
        if (
            Path(str(writer.get("plist_path") or "")).resolve() != expected_plist
            or writer.get("role") != "scheduled_writer"
            or "/bin/email_sync.py" not in (writer.get("process_markers") or [])
            or not expected_plist.is_file()
            or expected_plist.is_symlink()
        ):
            raise EmailSyncError("invalid_config", "email maintenance writer registration is invalid")
        try:
            with expected_plist.open("rb") as handle:
                plist = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException) as exc:
            raise EmailSyncError("invalid_config", "email writer plist is invalid") from exc
        if (
            plist.get("Label") != section["writer_label"]
            or plist.get("ProgramArguments") != [str(Path(__file__).resolve()), "sync-once"]
            or plist.get("RunAtLoad") is True
            or bool(plist.get("KeepAlive"))
            or "StartInterval" in plist
            or "StartCalendarInterval" in plist
        ):
            raise EmailSyncError("invalid_config", "email writer plist would create a second scheduler")
    config["_config_path"] = str(path.resolve())
    return config


def runtime_paths(config: dict[str, Any]) -> dict[str, Path]:
    section = config["email_sync"]
    external_root = Path(str(config["external_root"]))
    state_root = Path(str(section.get("state_root") or external_root / "email-sync"))
    archive_root = Path(str(section.get("archive_root") or external_root / "email-archive" / "automatic"))
    return {
        "external_root": external_root,
        "memory_root": Path(str(config["memory_root"])),
        "state_root": state_root,
        "database": state_root / "state" / "email-sync.sqlite",
        "runs": state_root / "runs",
        "staging": state_root / "staging",
        "failed_staging": state_root / "failed-staging",
        "archive_root": archive_root,
        "read_safety_receipt": Path(str(section["read_safety_receipt"])),
        "target_map": Path(str(section["target_map_path"])),
        "profile_root": Path(str(section["feishu_profile_root"])),
        "manual_hash_index": state_root / "state" / "manual-archive-hashes.json",
    }


def acquire_run_lock(blocking: bool = False):
    ensure_private_dir(RUN_LOCK.parent)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(RUN_LOCK, flags, 0o600)
    except OSError as exc:
        raise EmailSyncError("unsafe_run_lock", "email run lock is unavailable") from exc
    handle = os.fdopen(descriptor, "a+")
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError as exc:
        handle.close()
        raise EmailSyncError("already_running", "email sync is already running") from exc
    RUN_LOCK.chmod(0o600)
    return handle


def connect_database(config: dict[str, Any]) -> sqlite3.Connection:
    path = runtime_paths(config)["database"]
    ensure_private_dir(path.parent)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise EmailSyncError("unsafe_state_database", "email state database is unsafe")
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    if path.exists():
        path.chmod(0o600)
    return connection


def connect_database_readonly(config: dict[str, Any]) -> sqlite3.Connection:
    path = runtime_paths(config)["database"]
    if not path.is_file() or path.is_symlink():
        raise EmailSyncError("state_not_initialized", "email sync state is not initialized")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def initialize(config: dict[str, Any]) -> None:
    paths = runtime_paths(config)
    for key in ("state_root", "runs", "staging", "failed_staging", "archive_root"):
        ensure_private_dir(paths[key])
    connection = connect_database(config)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages(
              message_key TEXT PRIMARY KEY,
              mailbox TEXT NOT NULL,
              message_id TEXT NOT NULL,
              conversation_id TEXT,
              folder_id TEXT NOT NULL,
              direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
              received_at TEXT,
              size_bytes INTEGER NOT NULL DEFAULT 0,
              subject TEXT,
              subject_hash TEXT NOT NULL,
              archive_path TEXT,
              body_sha256 TEXT,
              manifest_sha256 TEXT,
              archive_state TEXT NOT NULL DEFAULT 'discovered',
              memory_state TEXT NOT NULL DEFAULT 'pending',
              report_state TEXT NOT NULL DEFAULT 'pending',
              first_batch_id TEXT NOT NULL,
              is_baseline INTEGER NOT NULL DEFAULT 0,
              archive_attempts INTEGER NOT NULL DEFAULT 0,
              archive_failure_attempts INTEGER NOT NULL DEFAULT 0,
              archive_failure_class TEXT,
              archive_failure_fingerprint TEXT,
              archive_source_fingerprint TEXT,
              archive_dws_sha256 TEXT,
              archive_validator_version TEXT,
              archive_repair_context TEXT,
              archive_next_retry_at TEXT,
              archive_retry_exhausted_at TEXT,
              semantic_attempts INTEGER NOT NULL DEFAULT 0,
              last_error_code TEXT,
              discovered_at TEXT NOT NULL,
              archived_at TEXT,
              memory_applied_at TEXT,
              reported_at TEXT,
              updated_at TEXT NOT NULL,
              UNIQUE(mailbox,message_id)
            );
            CREATE TABLE IF NOT EXISTS archive_recovery_history(
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              message_key TEXT NOT NULL, recorded_at TEXT NOT NULL,
              outcome TEXT NOT NULL, prior_failure_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attachments(
              message_key TEXT NOT NULL REFERENCES messages(message_key),
              attachment_id TEXT NOT NULL,
              original_name TEXT NOT NULL,
              stored_name TEXT NOT NULL,
              is_inline INTEGER NOT NULL DEFAULT 0,
              declared_size INTEGER NOT NULL DEFAULT 0,
              actual_size INTEGER,
              sha256 TEXT,
              risk TEXT NOT NULL DEFAULT 'unclassified',
              extract_status TEXT NOT NULL DEFAULT 'not_attempted',
              status TEXT NOT NULL DEFAULT 'pending',
              last_error_code TEXT,
              PRIMARY KEY(message_key,attachment_id)
            );
            CREATE TABLE IF NOT EXISTS batches(
              batch_id TEXT PRIMARY KEY,
              mode TEXT NOT NULL CHECK(mode IN ('baseline','incremental')),
              report_policy TEXT NOT NULL DEFAULT 'normal'
                CHECK(report_policy IN ('normal','local_only')),
              status TEXT NOT NULL DEFAULT 'discovered',
              discovered_count INTEGER NOT NULL DEFAULT 0,
              archived_count INTEGER NOT NULL DEFAULT 0,
              memory_applied_count INTEGER NOT NULL DEFAULT 0,
              failed_count INTEGER NOT NULL DEFAULT 0,
              report_revision INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              completed_at TEXT,
              last_error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS batch_messages(
              batch_id TEXT NOT NULL REFERENCES batches(batch_id),
              message_key TEXT NOT NULL REFERENCES messages(message_key),
              PRIMARY KEY(batch_id,message_key)
            );
            CREATE TABLE IF NOT EXISTS semantic_groups(
              semantic_id TEXT PRIMARY KEY,
              semantic_key TEXT NOT NULL UNIQUE,
              group_key TEXT NOT NULL,
              source_ref TEXT NOT NULL,
              batch_id TEXT NOT NULL REFERENCES batches(batch_id),
              status TEXT NOT NULL DEFAULT 'pending',
              proposal_path TEXT,
              input_sha256 TEXT NOT NULL,
              source_summary_written INTEGER NOT NULL DEFAULT 0,
              memory_writes INTEGER NOT NULL DEFAULT 0,
              pending_candidates INTEGER NOT NULL DEFAULT 0,
              linked_existing INTEGER NOT NULL DEFAULT 0,
              conflicts INTEGER NOT NULL DEFAULT 0,
              project_updates_json TEXT NOT NULL DEFAULT '[]',
              attempts INTEGER NOT NULL DEFAULT 0,
              last_error_code TEXT,
              created_at TEXT NOT NULL,
              completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS semantic_group_messages(
              semantic_id TEXT NOT NULL REFERENCES semantic_groups(semantic_id),
              message_key TEXT NOT NULL REFERENCES messages(message_key),
              source_token TEXT NOT NULL,
              PRIMARY KEY(semantic_id,message_key),
              UNIQUE(semantic_id,source_token)
            );
            CREATE TABLE IF NOT EXISTS outbox(
              event_id TEXT PRIMARY KEY,
              batch_id TEXT NOT NULL REFERENCES batches(batch_id),
              revision INTEGER NOT NULL,
              event_kind TEXT NOT NULL CHECK(event_kind IN ('baseline','incremental','correction')),
              status TEXT NOT NULL DEFAULT 'pending',
              is_complete INTEGER NOT NULL DEFAULT 0,
              report_text TEXT NOT NULL,
              event_hash TEXT NOT NULL UNIQUE,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT,
              receipt_hash TEXT,
              last_error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              delivered_at TEXT,
              UNIQUE(batch_id,revision)
            );
            CREATE TABLE IF NOT EXISTS meta(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_archive ON messages(archive_state,discovered_at);
            CREATE INDEX IF NOT EXISTS idx_messages_memory ON messages(memory_state,archived_at);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status,next_attempt_at);
            PRAGMA user_version=2;
            """
        )
        message_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")
        }
        if "report_state" not in message_columns:
            connection.execute(
                "ALTER TABLE messages ADD COLUMN report_state TEXT NOT NULL DEFAULT 'pending'"
            )
        if "reported_at" not in message_columns:
            connection.execute("ALTER TABLE messages ADD COLUMN reported_at TEXT")
        message_migrations = {
            "archive_failure_attempts": "INTEGER NOT NULL DEFAULT 0",
            "archive_failure_class": "TEXT",
            "archive_failure_fingerprint": "TEXT",
            "archive_source_fingerprint": "TEXT",
            "archive_dws_sha256": "TEXT",
            "archive_validator_version": "TEXT",
            "archive_repair_context": "TEXT",
            "archive_next_retry_at": "TEXT",
            "archive_retry_exhausted_at": "TEXT",
        }
        for column, declaration in message_migrations.items():
            if column not in message_columns:
                connection.execute(
                    f"ALTER TABLE messages ADD COLUMN {column} {declaration}"
                )
        outbox_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(outbox)")
        }
        if "is_complete" not in outbox_columns:
            connection.execute(
                "ALTER TABLE outbox ADD COLUMN is_complete INTEGER NOT NULL DEFAULT 0"
            )
        batch_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(batches)")
        }
        if "report_policy" not in batch_columns:
            connection.execute(
                "ALTER TABLE batches ADD COLUMN report_policy TEXT NOT NULL DEFAULT 'normal' "
                "CHECK(report_policy IN ('normal','local_only'))"
            )
        email_phases.initialize(connection)
        preapplied_keys = list(config["email_sync"]["preapplied_message_keys"])
        if preapplied_keys:
            placeholders = ",".join("?" for _ in preapplied_keys)
            stamp = now_iso()
            connection.execute(
                f"""
                UPDATE messages SET memory_state='applied',memory_applied_at=COALESCE(memory_applied_at,?),
                  last_error_code=NULL,updated_at=?
                WHERE message_key IN ({placeholders}) AND (phase_fingerprint IS NULL OR phase_fingerprint=phase_applied_fingerprint)
                """,
                (stamp, stamp, *preapplied_keys),
            )
        mail_auto_recovery.initialize(connection)
        connection.execute("PRAGMA user_version=5")
        connection.commit()
    finally:
        connection.close()
    runtime_paths(config)["database"].chmod(0o600)


def get_meta(connection: sqlite3.Connection, key: str) -> Optional[str]:
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def maintenance_active() -> bool:
    payload = read_json(MAINTENANCE_PATH, {})
    return isinstance(payload, dict) and payload.get("status") == "active"


def read_safety_status(config: dict[str, Any]) -> dict[str, Any]:
    path = runtime_paths(config)["read_safety_receipt"]
    try:
        safe_file = path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600
    except OSError:
        safe_file = False
    if not safe_file:
        return {"server_side_pass": False, "cross_device_confirmed": False}
    receipt = read_json(path, {})
    if not isinstance(receipt, dict):
        return {"server_side_pass": False, "cross_device_confirmed": False}
    try:
        current_dws_sha256 = sha256_file(dws_binary(config))
    except (EmailSyncError, OSError):
        current_dws_sha256 = ""
    binding_valid = (
        str(receipt.get("mailbox_hash") or "")
        == sha256_text(str(config["email_sync"]["mailbox"]))[:16]
        and bool(current_dws_sha256)
        and str(receipt.get("dws_sha256") or "") == current_dws_sha256
    )
    return {
        "server_side_pass": receipt.get("server_side_pass") is True and binding_valid,
        "cross_device_confirmed": receipt.get("cross_device_confirmed") is True and binding_valid,
        "binding_valid": binding_valid,
        "tested_at": receipt.get("tested_at"),
        "confirmed_at": receipt.get("cross_device_confirmed_at"),
    }


def confirm_read_safety(config: dict[str, Any], still_unread: bool) -> dict[str, Any]:
    path = runtime_paths(config)["read_safety_receipt"]
    if not path.is_file() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise EmailSyncError("read_safety_receipt_unsafe", "read-safety receipt is missing or unsafe")
    receipt = read_json(path, {})
    if not isinstance(receipt, dict) or receipt.get("server_side_pass") is not True:
        raise EmailSyncError("read_safety_server_unverified", "server-side read safety is not verified")
    if str(receipt.get("mailbox_hash") or "") != sha256_text(str(config["email_sync"]["mailbox"]))[:16]:
        raise EmailSyncError("read_safety_mailbox_mismatch", "read-safety receipt belongs to another mailbox")
    current = read_safety_status(config)
    if current.get("binding_valid") is not True or current.get("server_side_pass") is not True:
        raise EmailSyncError("read_safety_server_unverified", "current mailbox and client require a new server-side test")
    receipt["dws_sha256"] = sha256_file(dws_binary(config))
    receipt["cross_device_confirmed"] = bool(still_unread)
    receipt["cross_device_confirmed_at"] = now_iso()
    receipt["verdict"] = "pass" if still_unread else "failed"
    atomic_write_text(path, json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"server_side_pass": True, "cross_device_confirmed": bool(still_unread), "verdict": receipt["verdict"]}


def dws_binary(config: dict[str, Any]) -> Path:
    value = os.environ.get("EMAIL_SYNC_DWS_BIN") or config["email_sync"].get("dws_bin") or shutil.which("dws")
    path = Path(str(value or ""))
    if not path.is_file() or not os.access(path, os.X_OK):
        raise EmailSyncError("dws_missing", "configured dws binary is unavailable")
    return path


def run_dws(
    config: dict[str, Any],
    args: list[str],
    *,
    timeout: Optional[int] = None,
    cwd: Optional[Path] = None,
    expect_json: bool = True,
) -> Any:
    command = [str(dws_binary(config)), *args]
    if "--format" not in args:
        command.extend(["--format", "json"])
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout or int(config["email_sync"]["dws_timeout_seconds"]),
            check=False,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise EmailSyncError("dws_timeout", "mail read operation timed out") from exc
    if result.returncode != 0:
        values = parse_json_stream(result.stderr) + parse_json_stream(result.stdout)
        code = "dws_failed"
        for value in reversed(values):
            if isinstance(value, dict):
                error = value.get("error") if isinstance(value.get("error"), dict) else value
                candidate = error.get("reason") or error.get("code") if isinstance(error, dict) else None
                if candidate:
                    code = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(candidate))[:80]
                    break
        exc = EmailSyncError(code, "mail read operation failed")
        # Keep bounded, credential-free failure evidence for attachment recovery.
        # CLI exit codes alone do not identify a provider failure or allow retries.
        import chat_read_recovery
        exc.diagnostic = chat_read_recovery.failure_evidence(
            {"error": [v.get("error", v) for v in values if isinstance(v, dict)]}, code)
        raise exc
    if not expect_json:
        return result
    values = parse_json_stream(result.stdout)
    if not values or not isinstance(values[-1], dict):
        raise EmailSyncError("dws_invalid_json", "mail operation returned invalid JSON")
    return values[-1]


def real_messages(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    return [
        item for item in (payload.get("messages") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]


def paginate_messages(config: dict[str, Any], *, folder_id: Optional[str] = None, query: Optional[str] = None) -> list[dict[str, Any]]:
    if (folder_id is None) == (query is None):
        raise ValueError("exactly one of folder_id or query is required")
    mailbox = str(config["email_sync"]["mailbox"])
    limit = str(int(config["email_sync"]["max_page_size"]))
    cursor = ""
    seen_cursors: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    for _page in range(10000):
        args = ["mail", "message", "list" if folder_id is not None else "search", "--email", mailbox, "--limit", limit]
        if folder_id is not None:
            args.extend(["--folder-id", str(folder_id)])
        else:
            args.extend(["--query", str(query)])
        if cursor:
            args.extend(["--cursor", cursor])
        payload = run_dws(config, args)
        for item in real_messages(payload):
            normalized = dict(item)
            if folder_id is not None:
                normalized["_folder_id"] = str(folder_id)
            by_id[str(item["id"])] = normalized
        next_cursor = str(payload.get("nextCursor") or "")
        has_more = normalize_bool(payload.get("hasMore"))
        if next_cursor == "$" or (not has_more and not next_cursor):
            break
        if not next_cursor or next_cursor in seen_cursors:
            raise EmailSyncError("pagination_incomplete", "mail pagination did not reach a stable end")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise EmailSyncError("pagination_limit", "mail pagination exceeded the safety limit")
    return list(by_id.values())


def message_key(mailbox: str, message_id: str) -> str:
    return sha256_text(f"{mailbox}\x00{message_id}")[:32]


def conversation_key(mailbox: str, conversation_id: str, fallback_message_key: str) -> str:
    value = conversation_id.strip() or f"message:{fallback_message_key}"
    return sha256_text(f"{mailbox}\x00{value}")[:32]


def excluded_test_message(item: dict[str, Any]) -> bool:
    return TEST_SUBJECT_MARKER.lower() in str(item.get("subject") or "").lower()


def mailbox_preflight(config: dict[str, Any]) -> dict[str, Any]:
    payload = run_dws(config, ["mail", "mailbox", "list"])
    accounts = payload.get("emailAccounts") if isinstance(payload, dict) else None
    expected_mailbox = str(config["email_sync"]["mailbox"])
    matches = [
        row for row in accounts or []
        if isinstance(row, dict)
        and row.get("email") == expected_mailbox
        and row.get("type") == config["email_sync"]["mailbox_type"]
    ]
    if len(matches) != 1:
        raise EmailSyncError("mailbox_identity", "enterprise mailbox identity did not match")
    return {"mailbox_verified": True, "mailbox_hash": sha256_text(expected_mailbox)[:16]}


def discover_candidates(config: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    connection = connect_database(config)
    try:
        baseline_complete = get_meta(connection, "baseline_complete") == "true"
        last_incoming = get_meta(connection, "last_incoming_received_at")
        last_full_rescan = get_meta(connection, "last_full_rescan_at")
    finally:
        connection.close()
    mailbox = str(config["email_sync"]["mailbox"])
    if not baseline_complete:
        rows: list[dict[str, Any]] = []
        for folder_id in config["email_sync"]["baseline_folders"]:
            rows.extend(paginate_messages(config, folder_id=str(folder_id)))
        connection = connect_database(config)
        try:
            set_meta(connection, "last_full_rescan_at", now_iso())
            connection.commit()
        finally:
            connection.close()
        return "baseline", [row for row in rows if not excluded_test_message(row)]

    overlap = timedelta(hours=int(config["email_sync"]["overlap_hours"]))
    try:
        watermark = datetime.fromisoformat(str(last_incoming)) if last_incoming else datetime.now(timezone.utc) - overlap
    except ValueError:
        watermark = datetime.now(timezone.utc) - overlap
    if watermark.tzinfo is None:
        watermark = watermark.replace(tzinfo=timezone.utc)
    start = (watermark.astimezone(timezone.utc) - overlap).isoformat(timespec="seconds").replace("+00:00", "Z")
    incoming = paginate_messages(config, query=f"folderId:2 AND date>={start}")
    daily_due = True
    if last_full_rescan:
        try:
            daily_due = datetime.now().astimezone() - datetime.fromisoformat(last_full_rescan) >= timedelta(
                hours=int(config["email_sync"]["daily_rescan_hours"])
            )
        except ValueError:
            daily_due = True
    if daily_due:
        incoming = paginate_messages(config, folder_id="2")
        connection = connect_database(config)
        try:
            set_meta(connection, "last_full_rescan_at", now_iso())
            connection.commit()
        finally:
            connection.close()
    incoming = [row for row in incoming if not excluded_test_message(row)]

    connection = connect_database(config)
    try:
        unseen_incoming = [
            row for row in incoming
            if connection.execute(
                "SELECT 1 FROM messages WHERE mailbox=? AND message_id=?",
                (mailbox, str(row.get("id") or "")),
            ).fetchone() is None
        ]
    finally:
        connection.close()
    if not unseen_incoming:
        return "incremental", []
    conversation_ids = {str(row.get("conversationId") or "") for row in unseen_incoming if row.get("conversationId")}
    selected = list(unseen_incoming)
    if conversation_ids:
        for folder_id in config["email_sync"]["baseline_folders"]:
            for row in paginate_messages(config, folder_id=str(folder_id)):
                if str(row.get("conversationId") or "") in conversation_ids and not excluded_test_message(row):
                    selected.append(row)
    by_id = {str(row["id"]): row for row in selected if row.get("id")}
    return "incremental", list(by_id.values())


def register_discovery(
    config: dict[str, Any], mode: str, rows: list[dict[str, Any]], *, report_policy: str = "normal"
) -> tuple[Optional[str], int]:
    if report_policy not in {"normal", "local_only"}:
        raise EmailSyncError("report_policy_invalid", "email report policy is invalid")
    mailbox = str(config["email_sync"]["mailbox"])
    connection = connect_database(config)
    try:
        unseen: list[dict[str, Any]] = []
        unique_rows = {
            str(row.get("id")): row for row in rows
            if isinstance(row, dict) and str(row.get("id") or "")
        }
        for row in unique_rows.values():
            message_id = str(row.get("id") or "")
            if not message_id:
                continue
            if connection.execute(
                "SELECT 1 FROM messages WHERE mailbox=? AND message_id=?", (mailbox, message_id)
            ).fetchone() is None:
                unseen.append(row)
        if not unseen:
            return None, 0
        stamp = now_iso()
        connection.execute("BEGIN IMMEDIATE")
        active_baseline = None
        if mode == "baseline":
            active_baseline = connection.execute(
                """
                SELECT batch_id,status FROM batches
                WHERE mode='baseline' AND report_policy=?
                  AND status NOT IN ('reported','report_uncertain','local_only_complete')
                ORDER BY created_at LIMIT 1
                """
            , (report_policy,)).fetchone()
        if active_baseline:
            batch_id = str(active_baseline["batch_id"])
            connection.execute(
                "UPDATE batches SET discovered_count=discovered_count+? WHERE batch_id=?",
                (len(unseen), batch_id),
            )
        else:
            batch_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO batches(batch_id,mode,report_policy,discovered_count,created_at) "
                "VALUES(?,?,?,?,?)",
                (batch_id, mode, report_policy, len(unseen), stamp),
            )
        for row in unseen:
            message_id = str(row["id"])
            key = message_key(mailbox, message_id)
            memory_preapplied = key in set(config["email_sync"]["preapplied_message_keys"])
            folder_id = str(row.get("_folder_id") or ("2" if row.get("from") else "2"))
            direction = "outgoing" if folder_id == "1" else "incoming"
            subject = str(row.get("subject") or "")
            connection.execute(
                """
                INSERT INTO messages(
                  message_key,mailbox,message_id,conversation_id,folder_id,direction,received_at,
                  size_bytes,subject,subject_hash,memory_state,memory_applied_at,first_batch_id,
                  is_baseline,discovered_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key, mailbox, message_id, str(row.get("conversationId") or ""), folder_id, direction,
                    str(row.get("receivedDateTime") or ""), normalize_int(row.get("size")), subject,
                    sha256_text(subject), "applied" if memory_preapplied else "pending",
                    stamp if memory_preapplied else None, batch_id, 1 if mode == "baseline" else 0,
                    stamp, stamp,
                ),
            )
            connection.execute("INSERT INTO batch_messages(batch_id,message_key) VALUES(?,?)", (batch_id, key))
        incoming_times = [
            str(row.get("receivedDateTime") or "")
            for row in unseen
            if str(row.get("_folder_id") or "2") == "2" and row.get("receivedDateTime")
        ]
        if incoming_times:
            previous = get_meta(connection, "last_incoming_received_at") or ""
            set_meta(connection, "last_incoming_received_at", max([previous, *incoming_times]))
        connection.commit()
        return batch_id, len(unseen)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def eml_bodies(data: bytes, limit: int) -> tuple[str, str]:
    try:
        message = email.message_from_bytes(data, policy=policy.default)
    except Exception:
        return "", ""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = decode_text_bytes(payload)
        if not isinstance(content, str):
            continue
        if content_type == "text/plain":
            plain_parts.append(content)
        else:
            html_parts.append(content)
    plain = "\n\n".join(plain_parts)[:limit]
    raw_html = "\n\n".join(html_parts)[: max(limit * 4, limit)]
    if not plain and raw_html:
        plain = html_to_text(raw_html)[:limit]
    return plain, raw_html


def inspect_zip_xml(path: Path, limit: int) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 500:
                return "", "archive_entry_limit"
            total_uncompressed = sum(max(0, item.file_size) for item in entries)
            total_compressed = sum(max(1, item.compress_size) for item in entries)
            if total_uncompressed > 50 * 1024 * 1024 or total_uncompressed / total_compressed > 200:
                return "", "archive_expansion_risk"
            lowered_names = [item.filename.replace("\\", "/").lower() for item in entries]
            if any(
                name.endswith("vbaproject.bin")
                or "/embeddings/" in name
                or name.endswith((".exe", ".dll", ".js", ".vbs", ".cmd", ".ps1"))
                for name in lowered_names
            ):
                return "", "blocked_active_content"
            selected: list[zipfile.ZipInfo] = []
            for item in entries:
                name = item.filename.replace("\\", "/")
                if name.startswith("/") or "../" in f"/{name}":
                    return "", "archive_path_risk"
                if not name.lower().endswith(".xml"):
                    continue
                if name.startswith(("word/", "xl/sharedStrings", "xl/worksheets/", "ppt/slides/", "ppt/notesSlides/")):
                    selected.append(item)
            parts: list[str] = []
            used = 0
            for item in selected:
                if used >= limit * 4:
                    break
                raw = archive.read(item)
                used += len(raw)
                text = html.unescape(re.sub(r"<[^>]+>", " ", decode_text_bytes(raw)))
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    parts.append(text)
            return "\n".join(parts)[:limit], "extracted" if parts else "no_text"
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return "", "archive_invalid"


def extract_attachment_text(path: Path, limit: int) -> tuple[str, str, str]:
    video_hold.guard('dingtalk_mail',{'local_object':sha256_text(str(path))},name=path.name,existing=True)
    suffix = path.suffix.lower()
    if suffix in HAZARDOUS_EXTENSIONS:
        return "", "blocked_active_content", "hazardous"
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return "", "file_unavailable", "unknown"
    if (
        header.startswith((b"MZ", b"\x7fELF", b"#!"))
        or header[:4] in {b"\xca\xfe\xba\xbe", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf"}
    ):
        return "", "blocked_active_content", "hazardous"
    if suffix in ARCHIVE_EXTENSIONS:
        return "", "archive_not_opened", "archive"
    try:
        size = path.stat().st_size
    except OSError:
        return "", "file_unavailable", "unknown"
    if size > 100 * 1024 * 1024:
        return "", "file_too_large_for_extraction", "large"
    if suffix in TEXT_EXTENSIONS:
        try:
            return decode_text_bytes(path.read_bytes()[: limit * 4])[:limit], "extracted", "ordinary"
        except OSError:
            return "", "read_failed", "ordinary"
    if suffix in {".html", ".htm"}:
        try:
            return html_to_text(decode_text_bytes(path.read_bytes()[: limit * 8]))[:limit], "extracted", "ordinary"
        except OSError:
            return "", "read_failed", "ordinary"
    if suffix == ".eml":
        try:
            plain, raw_html = eml_bodies(path.read_bytes(), limit)
            return (plain or html_to_text(raw_html))[:limit], "extracted" if (plain or raw_html) else "no_text", "nested_email"
        except OSError:
            return "", "read_failed", "nested_email"
    if suffix in OFFICE_XML_EXTENSIONS:
        text, status_name = inspect_zip_xml(path, limit)
        risk = "ordinary" if status_name in {"extracted", "no_text"} else (
            "hazardous" if status_name == "blocked_active_content" else "archive_risk"
        )
        return text, status_name, risk
    if suffix == ".pdf":
        binary = next(
            (candidate for candidate in (Path("/opt/homebrew/bin/pdftotext"), Path("/usr/local/bin/pdftotext"), Path("/usr/bin/pdftotext")) if candidate.is_file()),
            None,
        )
        if not binary:
            return "", "pdf_parser_unavailable", "ordinary"
        try:
            result = subprocess.run(
                [str(binary), "-q", "-nopgbrk", str(path), "-"],
                text=True,
                capture_output=True,
                timeout=45,
                check=False,
                env={**os.environ, "LC_ALL": "en_US.UTF-8"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return "", "pdf_extract_failed", "ordinary"
        if result.returncode != 0:
            return "", "pdf_extract_failed", "ordinary"
        return result.stdout[:limit], "extracted" if result.stdout.strip() else "no_text", "ordinary"
    return "", "unsupported_for_text", "ordinary"


def final_archive_path(config: dict[str, Any], row: sqlite3.Row) -> Path:
    try:
        observed = datetime.fromisoformat(str(row["received_at"]).replace("Z", "+00:00"))
    except ValueError:
        observed = datetime.now().astimezone()
    return runtime_paths(config)["archive_root"] / observed.strftime("%Y") / observed.strftime("%m") / str(row["message_key"])


def verify_archive(path: Path, expected_key: str) -> tuple[bool, Optional[str]]:
    if not path.is_dir() or path.is_symlink():
        return False, "manifest_root"
    manifest = read_json(path / "manifest.json", {})
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in {1, 2, 3} or manifest.get("message_key") != expected_key:
        return False, "manifest_identity"
    files = manifest.get("files")
    if not isinstance(files, list):
        return False, "manifest_files"
    expected_paths = {"manifest.json", "manifest.sha256"}
    expected_lines: list[str] = []
    file_hashes: dict[str, str] = {}
    for entry in files:
        if not isinstance(entry, dict):
            return False, "manifest_entry"
        relative = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "")
        if relative in expected_paths or not re.fullmatch(r"[0-9a-f]{64}", expected):
            return False, "manifest_entry"
        target = path / relative
        try:
            resolved = target.resolve()
            resolved.relative_to(path.resolve())
            if not target.is_file() or target.is_symlink() or sha256_file(target) != expected:
                return False, "manifest_hash"
        except (OSError, ValueError):
            return False, "manifest_read"
        expected_paths.add(relative)
        file_hashes[relative] = expected
        expected_lines.append(f"{expected}  {relative}")
    try:
        body_text = (path / "body.md").read_text(encoding="utf-8")
    except OSError:
        return False, "manifest_body"
    if (
        manifest.get("body_complete") is not True
        or file_hashes.get("body.md") != sha256_file(path / "body.md")
        or str(manifest.get("body_sha256") or "") != sha256_text(body_text)
    ):
        return False, "manifest_body"
    attachments = manifest.get("attachments")
    if not isinstance(attachments, list):
        return False, "manifest_attachments"
    if manifest.get("schema_version") == 3:
        inventory = read_json(path / "attachment-inventory.json", None)
        if not isinstance(inventory, list) or len(inventory) != len(attachments):
            return False, "manifest_inventory"
        expected = {sha256_text(str(i.get("id") or ""))[:16]: i for i in inventory if isinstance(i, dict) and i.get("id")}
        if len(expected) != len(attachments): return False, "manifest_inventory"
        for a in attachments:
            i = expected.get(a.get("attachment_id_hash")) if isinstance(a, dict) else None
            if not i or a.get("original_name") != str(i.get("name") or "attachment") or a.get("declared_size") != normalize_int(i.get("size")):
                return False, "manifest_inventory"
    for attachment in attachments:
        if not isinstance(attachment, dict):
            return False, "manifest_attachment_entry"
        if manifest.get("schema_version") in {2, 3} or attachment.get("storage") == "video_hold":
            if not win11_attachments.verify_archived(
                path, attachment, file_hashes, manifest.get("attachment_policy") or {},
                read_json(path / "message.json", {}),
            ):
                return False, "manifest_remote_attachment_receipt"
            continue
        stored_name = str(attachment.get("stored_name") or "")
        digest = str(attachment.get("sha256") or "")
        target = path / stored_name
        try:
            if (
                not stored_name.startswith("attachments/")
                or file_hashes.get(stored_name) != digest
                or not target.is_file()
                or target.is_symlink()
                or target.stat().st_size != normalize_int(attachment.get("actual_size"), -1)
            ):
                return False, "manifest_attachment_entry"
        except OSError:
            return False, "manifest_attachment_entry"
    try:
        actual_paths: set[str] = set()
        for candidate in path.rglob("*"):
            if candidate.is_symlink():
                return False, "manifest_symlink"
            if candidate.is_file():
                actual_paths.add(str(candidate.relative_to(path)))
        if actual_paths != expected_paths:
            return False, "manifest_extra_or_missing"
        expected_lines.append(f"{sha256_file(path / 'manifest.json')}  manifest.json")
        checksum_lines = (path / "manifest.sha256").read_text(encoding="utf-8").splitlines()
        if checksum_lines != expected_lines:
            return False, "manifest_checksum_file"
    except OSError:
        return False, "manifest_read"
    return True, None


def manual_archive_hashes(config: dict[str, Any]) -> set[str]:
    paths = runtime_paths(config)
    index_path = paths["manual_hash_index"]
    cached = read_json(index_path, {})
    observed = parse_datetime(cached.get("scanned_at")) if isinstance(cached, dict) else None
    if observed and datetime.now().astimezone() - observed < timedelta(hours=24):
        hashes = cached.get("sha256")
        if isinstance(hashes, list) and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
            return set(hashes)
    archive_parent = paths["archive_root"].parent
    automatic_root = paths["archive_root"].resolve()
    values: set[str] = set()
    files_seen = 0
    if archive_parent.is_dir() and not archive_parent.is_symlink():
        for candidate in archive_parent.rglob("*"):
            try:
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                candidate.resolve().relative_to(automatic_root)
                continue
            except ValueError:
                pass
            except OSError:
                continue
            try:
                values.add(sha256_file(candidate))
                files_seen += 1
            except OSError:
                continue
    atomic_write_text(
        index_path,
        json.dumps(
            {"schema_version": 1, "scanned_at": now_iso(), "files_seen": files_seen, "sha256": sorted(values)},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
    )
    return values


def export_eml_if_supported(config: dict[str, Any], row: sqlite3.Row, stage: Path) -> tuple[Optional[Path], str]:
    if int(row["size_bytes"] or 0) > 100 * 1024:
        return None, "size_limit"
    try:
        run_dws(
            config,
            [
                "mail", "message", "export", "--email", str(row["mailbox"]), "--id", str(row["message_id"]),
                "--filename", "original", "--overwrite",
            ],
            timeout=180,
            cwd=stage,
            expect_json=False,
        )
    except EmailSyncError as exc:
        return None, exc.code
    path = stage / "original.eml"
    if not path.is_file():
        return None, "export_output_missing"
    path.chmod(0o600)
    return path, "exported"


def failed_stage_fingerprint(stage: Path, key: str, error_code: str) -> str:
    files: list[list[Any]] = []
    for candidate in sorted(stage.rglob("*")):
        if candidate.is_symlink():
            raise EmailSyncError("archive_stage_symlink", "mail archive staging contains a symlink")
        if candidate.is_file():
            files.append([
                str(candidate.relative_to(stage)),
                candidate.stat().st_size,
                sha256_file(candidate),
            ])
    return sha256_text(canonical_json([key, error_code, files]))


def move_failed_stage(
    config: dict[str, Any], stage: Path, key: str, error_code: str = "archive_failed"
) -> Optional[Path]:
    if not stage.exists():
        return None
    fingerprint = failed_stage_fingerprint(stage, key, error_code)
    destination = runtime_paths(config)["failed_staging"] / f"{key}-{fingerprint[:24]}"
    ensure_private_dir(destination.parent)
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise EmailSyncError("failed_stage_unsafe", "failed evidence destination is unsafe")
        shutil.rmtree(stage)
        return destination
    os.replace(stage, destination)
    return destination


def archive_source_fingerprint(row: sqlite3.Row) -> str:
    return sha256_text(canonical_json([
        str(row["mailbox"]),
        str(row["message_id"]),
        str(row["conversation_id"] or ""),
        str(row["folder_id"]),
        str(row["received_at"] or ""),
        int(row["size_bytes"] or 0),
        str(row["subject_hash"] or ""),
    ]))


def dws_runtime_fingerprint(config: dict[str, Any]) -> str:
    try:
        return sha256_file(dws_binary(config))
    except (EmailSyncError, OSError):
        return "dws-unavailable"


def archive_failure_class(code: str) -> str:
    normalized = str(code or "").lower().replace("-", "_")
    if any(token in normalized for token in ("authentication", "permission", "access_denied", "unauthorized", "forbidden")):
        return "deterministic"
    transient_tokens = (
        "timeout", "rate_limit", "ratelimit", "too_many_requests",
        "resource_exhausted", "deadline_exceeded", "service_unavailable",
        "service_busy", "service_error", "internal_server_error", "server_error",
        "temporarily_unavailable", "overloaded", "upstream_unavailable",
        "connection_reset", "connection_refused", "network_unavailable", "bad_gateway",
        "gateway_timeout", "http_429", "http_500", "http_502", "http_503",
        "http_504", "dws_invalid_json",
        "win11_download_failed", "win11_reader_unavailable", "attachment_session_unavailable",
    )
    return "transient" if any(token in normalized for token in transient_tokens) else "deterministic"


# Only this known stale source-access failure may be retried after its own
# implementation/policy changes. This permits rechecking, never accepting data.
ARCHIVE_REPAIRABLE_CODES = frozenset({"content_source_access_changed"})


def archive_repair_context(config: dict[str, Any]) -> Optional[str]:
    try:
        files = ["email_sync.py", "email_phases.py", "win11_mail_attachments.py",
                 "content_registry.py", "content_identity.py"]
        hashes = [(name, sha256_file(BRIDGE_ROOT / "bin" / name)) for name in files]
        section = config.get("email_sync") or {}
        policy = win11_attachments.content_registry.policy(config)
        return sha256_text(canonical_json([hashes, section.get("phased_processing"),
                                          section.get("attachment_processing"), policy]))
    except (OSError, ValueError, win11_attachments.AccessDenied):
        # Missing or denied context never grants a new attempt.
        return None


def retain_archive_failure(connection: sqlite3.Connection, message_key: str, outcome: str) -> None:
    row = connection.execute("SELECT * FROM messages WHERE message_key=?", (message_key,)).fetchone()
    if row is None or row["archive_state"] != "failed":
        return
    fields = ("archive_attempts", "archive_failure_attempts", "archive_failure_class",
              "archive_failure_fingerprint", "archive_source_fingerprint", "archive_dws_sha256",
              "archive_validator_version", "archive_repair_context", "archive_next_retry_at",
              "archive_retry_exhausted_at", "last_error_code", "updated_at")
    connection.execute("INSERT INTO archive_recovery_history(message_key,recorded_at,outcome,prior_failure_json) VALUES(?,?,?,?)",
                       (message_key, now_iso(), outcome, canonical_json({key: row[key] for key in fields})))


def archive_failure_fingerprint(
    row: sqlite3.Row, code: str, source_fingerprint: str, dws_sha256: str
) -> str:
    return sha256_text(canonical_json([
        str(row["message_key"]), str(code), source_fingerprint,
        dws_sha256, BODY_LENGTH_VALIDATOR_VERSION,
    ]))


def archive_retry_due(
    row: sqlite3.Row,
    *,
    dws_sha256: str,
    at: Optional[datetime] = None,
    repair_context: Optional[str] = None,
) -> bool:
    if row["archive_state"] == "discovered":
        return True
    if row["archive_state"] != "failed":
        return False
    if (row["last_error_code"] in ARCHIVE_REPAIRABLE_CODES and repair_context
            and row["archive_repair_context"] != repair_context):
        return True
    source_fingerprint = archive_source_fingerprint(row)
    if (
        row["archive_source_fingerprint"] != source_fingerprint
        or row["archive_dws_sha256"] != dws_sha256
        or row["archive_validator_version"] != BODY_LENGTH_VALIDATOR_VERSION
    ):
        return True
    if row["archive_failure_class"] != "transient" or archive_failure_class(row["last_error_code"]) != "transient":
        return False
    # Exhaustion closes the short retry burst; it does not abandon a transient
    # read forever. Preserve the original exhaustion marker and cumulative count.
    value = str(row["archive_next_retry_at"] or row["archive_retry_exhausted_at"] or "")
    if not value:
        return False
    try:
        due = datetime.fromisoformat(value)
    except ValueError:
        return False
    if not row["archive_next_retry_at"]:
        due += timedelta(seconds=ARCHIVE_PERSISTENT_PROBE_SECONDS)
    current = at or datetime.now().astimezone()
    if due.tzinfo is None:
        due = due.replace(tzinfo=current.tzinfo)
    return due <= current


def attachment_inventory_name(index: int, original_name: str) -> str:
    suffix = Path(original_name.rstrip(" .")).suffix[:16]
    suffix = CONTROL_RE.sub("", suffix)
    suffix = re.sub(r"[^A-Za-z0-9._-]", "-", suffix)
    stem = safe_filename(Path(original_name).stem, 70)
    return f"{index:03d}-{stem}{suffix}"


def record_attachment_inventory(
    config: dict[str, Any], row: sqlite3.Row, attachments: list[dict[str, Any]]
) -> None:
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM attachments WHERE message_key=?", (str(row["message_key"]),))
        for index, item in enumerate(attachments, 1):
            attachment_id = str(item.get("id") or "")
            if not attachment_id:
                raise EmailSyncError("attachment_identity", "attachment identity is incomplete")
            original_name = str(item.get("name") or "attachment")
            connection.execute(
                """
                INSERT INTO attachments(
                  message_key,attachment_id,original_name,stored_name,is_inline,declared_size,
                  risk,extract_status,status,last_error_code
                ) VALUES(?,?,?,?,?,?,'unclassified','not_attempted','pending',NULL)
                """,
                (
                    str(row["message_key"]), sha256_text(attachment_id), original_name,
                    str(Path("attachments") / attachment_inventory_name(index, original_name)),
                    1 if normalize_bool(item.get("isInline")) else 0, normalize_int(item.get("size")),
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def archive_one(config: dict[str, Any], row: sqlite3.Row) -> dict[str, Any]:
    final = final_archive_path(config, row)
    if final.exists():
        valid, reason = verify_archive(final, str(row["message_key"]))
        if not valid:
            move_failed_stage(config, final, str(row["message_key"]), reason or "archive_invalid")
            raise EmailSyncError(reason or "archive_invalid", "existing mail archive failed verification")
        manifest = read_json(final / "manifest.json", {})
        return {
            "archive_path": str(final),
            "body_sha256": str(manifest.get("body_sha256") or ""),
            "manifest_sha256": sha256_file(final / "manifest.json"),
            "attachments": manifest.get("attachments") or [],
            "reused": True,
        }

    paths = runtime_paths(config)
    stage = paths["staging"] / f"{uuid.uuid4()}-{row['message_key']}"
    ensure_private_dir(stage)
    ensure_private_dir(stage / "attachments")
    ensure_private_dir(stage / "attachment-text")
    try:
        payload = run_dws(
            config,
            ["mail", "message", "get", "--email", str(row["mailbox"]), "--id", str(row["message_id"])],
        )
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict) or str(message.get("id") or "") != str(row["message_id"]):
            raise EmailSyncError("message_identity", "mail body identity did not match")
        markdown_body = str(message.get("markdownBody") or "")
        atomic_write_text(stage / "message.json", json.dumps(message, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        atomic_write_text(stage / "body.md", markdown_body)
        atomic_write_text(stage / "body.txt", re.sub(r"\s+", " ", markdown_body).strip() + "\n")
        normalized_html = (
            "<!doctype html><html><head><meta charset=\"utf-8\"></head><body><pre>"
            + html.escape(markdown_body)
            + "</pre></body></html>\n"
        )
        atomic_write_text(stage / "body.html", normalized_html)

        attachment_policy = win11_attachments.policy(config)
        # EML contains original MIME attachments, so exporting it to M2 would
        # violate the same boundary as a direct attachment download.
        if attachment_policy:
            eml_path, eml_status = None, "omitted_by_win11_attachment_policy"
        else:
            eml_path, eml_status = export_eml_if_supported(config, row, stage)
        expected_eml_size = int(row["size_bytes"] or 0)
        eml_size_bytes = eml_path.stat().st_size if eml_path else None
        eml_size_match = bool(
            eml_path
            and (expected_eml_size <= 0 or int(eml_size_bytes or 0) == expected_eml_size)
        )
        length_evidence = body_length_evidence(
            message,
            markdown_body,
            complete_eml_export=eml_size_match,
        )
        original_html_available = False
        if eml_path:
            plain, raw_html = eml_bodies(eml_path.read_bytes(), int(config["email_sync"]["max_message_chars"]))
            if plain:
                atomic_write_text(stage / "body-from-eml.txt", plain)
            if raw_html:
                atomic_write_text(stage / "body-original.html", raw_html)
                original_html_available = True

        attachment_payload = run_dws(
            config,
            ["mail", "attachment", "list", "--email", str(row["mailbox"]), "--id", str(row["message_id"])],
        )
        raw_attachments = attachment_payload.get("attachments") if isinstance(attachment_payload, dict) else None
        if not isinstance(raw_attachments, list):
            raise EmailSyncError("attachment_list_shape", "attachment list is invalid")
        record_attachment_inventory(config, row, raw_attachments)
        phased = email_phases.enabled(config)
        if phased:
            if not attachment_policy:
                raise EmailSyncError("attachment_policy_missing", "Staged mail requires the Win11 policy")
            atomic_write_text(stage / "attachment-inventory.json", canonical_json(raw_attachments))
        attachments: list[dict[str, Any]] = []
        skip_self = bool(attachment_policy and raw_attachments
                         and win11_attachments.sender_is_self(message, attachment_policy))
        for index, item in enumerate(raw_attachments, 1):
            if not isinstance(item, dict) or not str(item.get("id") or ""):
                raise EmailSyncError("attachment_identity", "attachment identity is incomplete")
            original_name = str(item.get("name") or "attachment")
            if phased:
                attachments.append(email_phases.pending_entry(item, index, message, attachment_policy))
                continue
            hold_kind=video_hold.classify(original_name, item.get('contentType') or item.get('mimeType',''))
            if hold_kind and not skip_self:
                aid=sha256_text(str(item['id']))[:16]
                video_hold.register('dingtalk_mail', {'message_key':str(row['message_key']),'attachment_id':aid},
                    name=original_name,size=normalize_int(item.get('size')),kind=hold_kind)
                attachments.append({'attachment_id_hash':aid,'original_name':original_name,
                    'is_inline':normalize_bool(item.get('isInline')),'declared_size':normalize_int(item.get('size')),
                    'storage':'video_hold','stored_name':'policy-held/video/'+aid,'actual_size':None,'sha256':'',
                    'risk':'policy_pending','extract_status':'waiting_instruction','extracted_text_path':None,
                    'video_policy':video_hold.POLICY,'declared_content_type':item.get('contentType') or item.get('mimeType','')})
                continue
            if attachment_policy:
                common = {
                    "attachment_id_hash": sha256_text(str(item["id"]))[:16],
                    "original_name": original_name,
                    "is_inline": normalize_bool(item.get("isInline")),
                    "declared_size": normalize_int(item.get("size")),
                }
                if skip_self:
                    attachments.append({**common, "storage": "skipped_self_sender",
                        "stored_name": "policy-skipped/self-sender/" + common["attachment_id_hash"],
                        "actual_size": None, "sha256": "", "risk": "policy_exempt",
                        "extract_status": "skipped_self_sender", "extracted_text_path": None})
                    continue
                remote = win11_attachments.fetch(config, row, item)
                text = remote.pop("text", "")
                receipt_relative = Path("attachment-receipts") / f"{index:03d}.json"
                atomic_write_text(stage / receipt_relative, json.dumps(remote, ensure_ascii=False, indent=2) + "\n")
                text_relative = None
                if text:
                    text_relative = str(Path("attachment-text") / f"{index:03d}.txt")
                    atomic_write_text(stage / text_relative, text)
                attachments.append({**common, "storage": "win11",
                    "stored_name": "win11/" + remote["job_id"] + "/" + Path(remote["remote_path"].replace("\\", "/")).name,
                    "actual_size": remote["source_bytes"], "sha256": remote["source_sha256"],
                    "identity_sha256": remote["identity_sha256"], "worker_sha256": remote["worker_sha256"],
                    "risk": remote["risk"], "extract_status": remote["extract_status"],
                    "receipt_path": str(receipt_relative), "extracted_text_path": text_relative})
                continue
            stored_name = attachment_inventory_name(index, original_name)
            output_dir = stage / "attachments"
            result = run_dws(
                config,
                [
                    "mail", "attachment", "download", "--email", str(row["mailbox"]),
                    "--message-id", str(row["message_id"]), "--attachment-id", str(item["id"]),
                    "--name", stored_name, "--output", str(output_dir),
                ],
                timeout=300,
                expect_json=False,
            )
            del result
            target = output_dir / stored_name
            if not target.is_file() or target.is_symlink():
                raise EmailSyncError("attachment_download_missing", "downloaded attachment is missing")
            target.chmod(0o600)
            actual_size = target.stat().st_size
            declared_size = normalize_int(item.get("size"))
            if declared_size and actual_size != declared_size:
                raise EmailSyncError("attachment_size_mismatch", "downloaded attachment size did not match")
            extracted, extract_status, risk = extract_attachment_text(
                target, int(config["email_sync"]["max_attachment_text_chars"])
            )
            extracted_path: Optional[str] = None
            if extracted.strip():
                relative = Path("attachment-text") / f"{index:03d}.txt"
                atomic_write_text(stage / relative, extracted)
                extracted_path = str(relative)
            attachments.append(
                {
                    "attachment_id_hash": sha256_text(str(item["id"]))[:16],
                    "original_name": original_name,
                    "stored_name": str(Path("attachments") / stored_name),
                    "is_inline": normalize_bool(item.get("isInline")),
                    "declared_size": declared_size,
                    "actual_size": actual_size,
                    "sha256": sha256_file(target),
                    "risk": risk,
                    "extract_status": extract_status,
                    "extracted_text_path": extracted_path,
                    "coverage": {"complete": False, "scope": "legacy_fallback_limited_text",
                        "character_limit": int(config["email_sync"]["max_attachment_text_chars"]),
                        "returned_characters": len(extracted), "reason": "legacy_parser_has_no_full_coverage_receipt"},
                }
            )

        historical_hashes = manual_archive_hashes(config)
        file_entries: list[dict[str, Any]] = []
        stage_files: list[Path] = []
        for candidate in stage.rglob("*"):
            if candidate.is_symlink():
                raise EmailSyncError("archive_stage_symlink", "mail archive staging contains a symlink")
            if candidate.is_file() and candidate.name != "manifest.json":
                stage_files.append(candidate)
        for file_path in sorted(stage_files):
            file_path.chmod(0o600)
            digest = sha256_file(file_path)
            file_entries.append(
                {
                    "path": str(file_path.relative_to(stage)),
                    "sha256": digest,
                    "historical_hash_duplicate": digest in historical_hashes,
                }
            )
        manifest = {
            "schema_version": 3 if phased else (2 if attachment_policy else 1),
            "message_key": str(row["message_key"]),
            "mailbox_hash": sha256_text(str(row["mailbox"]))[:16],
            "message_id_hash": sha256_text(str(row["message_id"]))[:16],
            "conversation_hash": sha256_text(str(row["conversation_id"] or row["message_key"]))[:16],
            "folder_id": str(row["folder_id"]),
            "direction": str(row["direction"]),
            "received_at": str(row["received_at"] or ""),
            "body_sha256": sha256_text(markdown_body),
            "body_complete": True,
            **length_evidence,
            "phase_reference_gaps": email_phases.image_reference_gaps(markdown_body,raw_attachments) if phased else 0,
            "body_format": "markdown",
            "html_available": True,
            "html_source": "normalized_from_markdown",
            "original_html_available": original_html_available,
            "eml_status": eml_status,
            "eml_size_bytes": eml_size_bytes,
            "eml_size_match": eml_size_match,
            "attachments": attachments,
            "files": file_entries,
            "created_at": now_iso(),
        }
        if attachment_policy:
            manifest["attachment_policy"] = (email_phases.envelope(message, attachment_policy) if phased
                                             else win11_attachments.policy_receipt(message, attachment_policy))
        atomic_write_text(stage / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        checksum_lines = [f"{entry['sha256']}  {entry['path']}" for entry in file_entries]
        checksum_lines.append(f"{sha256_file(stage / 'manifest.json')}  manifest.json")
        atomic_write_text(stage / "manifest.sha256", "\n".join(checksum_lines) + "\n")
        ensure_private_dir(final.parent)
        os.replace(stage, final)
        valid, reason = verify_archive(final, str(row["message_key"]))
        if not valid:
            move_failed_stage(config, final, str(row["message_key"]), reason or "archive_verify")
            raise EmailSyncError(reason or "archive_verify", "committed archive failed verification")
        return {
            "archive_path": str(final),
            "body_sha256": manifest["body_sha256"],
            "manifest_sha256": sha256_file(final / "manifest.json"),
            "attachments": attachments,
            "reused": False,
        }
    except Exception as exc:
        move_failed_stage(
            config,
            stage,
            str(row["message_key"]),
            str(getattr(exc, "code", type(exc).__name__)),
        )
        if isinstance(exc, win11_attachments.AttachmentError):
            raise EmailSyncError(exc.code, "附件按 Win11 处理规则暂未完成；未转到 M2 下载或解析。") from exc
        raise


def retry_attachment_on_windows(config: dict[str, Any], attachment: dict[str, Any]) -> dict[str, Any]:
    """Reprocess one exact source attachment, without relying on a Mac original."""
    value = win11_attachments.policy(config)
    if not value:
        raise EmailSyncError("attachment_policy_missing", "Win11 附件处理规则尚未启用。")
    safety = read_safety_status(config)
    if not safety.get("server_side_pass") or not safety.get("cross_device_confirmed"):
        raise EmailSyncError("read_safety_unconfirmed", "邮件读取条件尚未核验，未重新读取。")
    connection = connect_database_readonly(config)
    try:
        row = connection.execute("SELECT * FROM messages WHERE message_key=?", (attachment["message_key"],)).fetchone()
        if not row:
            raise EmailSyncError("message_identity", "邮件来源记录不可用。")
        row = dict(row)
    finally:
        connection.close()
    payload = run_dws(config, ["mail", "message", "get", "--email", row["mailbox"], "--id", row["message_id"]])
    message = payload.get("message") or {}
    if message.get("id") != row["message_id"]:
        raise EmailSyncError("message_identity", "邮件来源身份不一致。")
    if win11_attachments.sender_is_self(message, value):
        return {"extract_status": "skipped_self_sender", "text": "", "original_received_on_m2": False,
                "policy": win11_attachments.policy_receipt(message, value)}
    listed = run_dws(config, ["mail", "attachment", "list", "--email", row["mailbox"], "--id", row["message_id"]])
    matches = [item for item in listed.get("attachments") or [] if isinstance(item, dict)
               and attachment["attachment_id"] in {sha256_text(str(item.get("id") or "")), sha256_text(str(item.get("id") or ""))[:16]}
               and item.get("name") == attachment["original_name"]]
    if len(matches) != 1:
        raise EmailSyncError("attachment_identity", "附件来源未能唯一核对。")
    result = win11_attachments.fetch(config, row, matches[0])
    if attachment.get("sha256") and result["source_sha256"] != attachment["sha256"]:
        raise EmailSyncError("attachment_identity", "来源附件内容已经变化，旧问题不能直接关闭。")
    return result


def mark_archive_success(config: dict[str, Any], row: sqlite3.Row, result: dict[str, Any]) -> None:
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        retain_archive_failure(connection, str(row["message_key"]), "archived")
        connection.execute("DELETE FROM attachments WHERE message_key=?", (str(row["message_key"]),))
        for index, item in enumerate(result.get("attachments") or [], 1):
            attachment_hash = str(item.get("attachment_id_hash") or f"missing-{index}")
            risk = str(item.get("risk") or "unknown")
            extract_status = str(item.get("extract_status") or "unknown")
            attachment_status = "needs_review" if (
                extract_status not in {"extracted", "extracted_static", "skipped_self_sender"}
                or risk in {"hazardous", "archive", "large", "archive_risk", "unknown"}
                or extract_status in {
                    "archive_entry_limit", "archive_expansion_risk", "archive_invalid",
                    "archive_path_risk", "blocked_active_content", "file_unavailable",
                    "pdf_extract_failed", "read_failed", "unknown",
                }
            ) else "verified"
            if item.get("storage") == "phase_pending":
                attachment_status = "pending"
            connection.execute(
                """
                INSERT INTO attachments(
                  message_key,attachment_id,original_name,stored_name,is_inline,declared_size,
                  actual_size,sha256,risk,extract_status,status,last_error_code
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)
                """,
                (
                    str(row["message_key"]), attachment_hash, str(item.get("original_name") or ""),
                    str(item.get("stored_name") or ""), 1 if item.get("is_inline") else 0,
                    normalize_int(item.get("declared_size")), normalize_int(item.get("actual_size")),
                    str(item.get("sha256") or ""), risk,
                    extract_status, attachment_status,
                ),
            )
        connection.execute(
            """
            UPDATE messages SET archive_path=?,body_sha256=?,manifest_sha256=?,archive_state='archived',
              last_error_code=NULL,archive_failure_attempts=0,archive_failure_class=NULL,
              archive_failure_fingerprint=NULL,archive_source_fingerprint=NULL,
              archive_dws_sha256=NULL,archive_validator_version=?,archive_next_retry_at=NULL,
              archive_retry_exhausted_at=NULL,archived_at=?,updated_at=? WHERE message_key=?
            """,
            (
                str(result["archive_path"]), str(result["body_sha256"]), str(result["manifest_sha256"]),
                BODY_LENGTH_VALIDATOR_VERSION, now_iso(), now_iso(), str(row["message_key"]),
            ),
        )
        phase_manifest=read_json(Path(str(result['archive_path']))/'manifest.json',{})
        connection.execute('UPDATE messages SET phase_reference_gaps=? WHERE message_key=?',
            (int(phase_manifest.get('phase_reference_gaps') or 0),str(row['message_key'])))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_archive_failure(
    config: dict[str, Any], row: sqlite3.Row | str, code: str, dws_sha256: Optional[str] = None,
    repair_context: Optional[str] = None,
) -> None:
    # Keep the older message-key call form working for local maintenance tools.
    if isinstance(row, str):
        lookup = connect_database(config)
        try:
            found = lookup.execute(
                "SELECT * FROM messages WHERE message_key=?", (row,)
            ).fetchone()
        finally:
            lookup.close()
        if found is None:
            raise EmailSyncError("message_missing", "邮件记录不存在，无法登记归档失败。")
        row = found
    dws_sha256 = dws_sha256 or dws_runtime_fingerprint(config)
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp = now_iso()
        source_fingerprint = archive_source_fingerprint(row)
        fingerprint = archive_failure_fingerprint(row, code, source_fingerprint, dws_sha256)
        current = connection.execute(
            "SELECT archive_failure_fingerprint,archive_failure_attempts,archive_retry_exhausted_at FROM messages WHERE message_key=?",
            (str(row["message_key"]),),
        ).fetchone()
        retain_archive_failure(connection, str(row["message_key"]), "failed")
        same_failure = bool(current and current["archive_failure_fingerprint"] == fingerprint)
        failure_attempts = int(current["archive_failure_attempts"] or 0) + 1 if same_failure else 1
        failure_class = archive_failure_class(code)
        next_retry_at: Optional[str] = None
        exhausted_at: Optional[str] = None
        if failure_class == "transient" and failure_attempts <= len(TRANSIENT_ARCHIVE_RETRY_DELAYS):
            delay = TRANSIENT_ARCHIVE_RETRY_DELAYS[failure_attempts - 1]
            next_retry_at = (datetime.now().astimezone() + timedelta(seconds=delay)).isoformat(timespec="seconds")
        else:
            exhausted_at = (current["archive_retry_exhausted_at"] if same_failure else None) or stamp
            if failure_class == "transient":
                next_retry_at = (datetime.now().astimezone() + timedelta(seconds=ARCHIVE_PERSISTENT_PROBE_SECONDS)).isoformat(timespec="seconds")
        connection.execute(
            """
            UPDATE messages SET archive_state='failed',archive_attempts=archive_attempts+1,
              archive_failure_attempts=?,archive_failure_class=?,archive_failure_fingerprint=?,
              archive_source_fingerprint=?,archive_dws_sha256=?,archive_validator_version=?,archive_repair_context=?,
              archive_next_retry_at=?,archive_retry_exhausted_at=?,last_error_code=?,updated_at=?
            WHERE message_key=?
            """,
            (
                failure_attempts, failure_class, fingerprint, source_fingerprint, dws_sha256,
                BODY_LENGTH_VALIDATOR_VERSION, repair_context or archive_repair_context(config), next_retry_at, exhausted_at, code, stamp,
                str(row["message_key"]),
            ),
        )
        connection.execute(
            "UPDATE attachments SET last_error_code=? WHERE message_key=? AND status='pending'",
            (code, str(row["message_key"])),
        )
        connection.commit()
    finally:
        connection.close()


def archive_pending(config: dict[str, Any], batch_id: Optional[str] = None) -> dict[str, int]:
    connection = connect_database(config)
    try:
        if batch_id:
            rows = connection.execute(
                """
                SELECT m.* FROM messages m JOIN batch_messages bm ON bm.message_key=m.message_key
                WHERE bm.batch_id=? AND m.archive_state IN ('discovered','failed')
                ORDER BY m.received_at,m.message_key
                """,
                (batch_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM messages WHERE archive_state IN ('discovered','failed') ORDER BY discovered_at,message_key"
            ).fetchall()
    finally:
        connection.close()
    dws_sha256 = dws_runtime_fingerprint(config)
    repair_context = archive_repair_context(config)
    recovery_grants = mail_auto_recovery.archive_grants(config, rows)
    if mail_auto_recovery.enabled(config):
        due_rows = [row for row in rows if row['archive_state']=='discovered' or str(row['message_key']) in recovery_grants]
    else:
        due_rows = [row for row in rows if archive_retry_due(row, dws_sha256=dws_sha256, repair_context=repair_context)]
    due_keys = {str(row["message_key"]) for row in due_rows}
    deferred = sum(
        row["archive_state"] == "failed"
        and row["archive_failure_class"] == "transient"
        and archive_failure_class(row["last_error_code"]) == "transient"
        and str(row["message_key"]) not in due_keys
        for row in rows
    )
    frozen = len(rows) - len(due_rows) - deferred
    archived = 0
    failed = 0
    attempted = 0
    idle_deferred = 0
    big_ready: Optional[bool] = None
    for row in due_rows:
        if not email_phases.enabled(config) and not message_is_light(config, row):
            if big_ready is None:
                big_ready = big_mail_ready(config)
            if not big_ready:
                idle_deferred += 1  # stays queued; retried when Win11 is on power
                continue
        attempted += 1
        try:
            result = archive_one(config, row)
            mark_archive_success(config, row, result)
            mail_auto_recovery.recovered(config, str(row["message_key"]), "archive", {"archive_verified": True, "manifest_sha256": result["manifest_sha256"]})
            archived += 1
        except (EmailSyncError, OSError, sqlite3.Error, ValueError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            mark_archive_failure(config, row, code, dws_sha256, repair_context)
            mail_auto_recovery.observe(config, str(row["message_key"]), "archive", archive_source_fingerprint(row), code, attempted=True)
            failed += 1
    connection = connect_database(config)
    try:
        if batch_id:
            unresolved = int(connection.execute(
                """
                SELECT COUNT(*) FROM messages m JOIN batch_messages bm ON bm.message_key=m.message_key
                WHERE bm.batch_id=? AND m.archive_state='failed'
                """,
                (batch_id,),
            ).fetchone()[0])
        else:
            unresolved = int(connection.execute(
                "SELECT COUNT(*) FROM messages WHERE archive_state='failed'"
            ).fetchone()[0])
    finally:
        connection.close()
    return {
        "attempted": attempted, "archived": archived, "failed": failed, "idle_deferred": idle_deferred,
        "deferred": deferred, "frozen": frozen, "unresolved": unresolved,
    }


def resolved_under(root: Path, relative: str) -> Optional[Path]:
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root.resolve())
        return candidate
    except (OSError, ValueError):
        return None


def full_conversation_rows(config: dict[str, Any], row: sqlite3.Row) -> list[sqlite3.Row]:
    connection = connect_database(config)
    try:
        conversation_id = str(row["conversation_id"] or "")
        if conversation_id:
            rows = connection.execute(
                "SELECT * FROM messages WHERE conversation_id=? AND archive_state='archived' ORDER BY received_at,message_key",
                (conversation_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM messages WHERE message_key=? AND archive_state='archived'", (str(row["message_key"]),)
            ).fetchall()
        preapplied_keys = set(config["email_sync"]["preapplied_message_keys"])
        return [item for item in rows if str(item["message_key"]) not in preapplied_keys]
    finally:
        connection.close()


def semantic_key_for_rows(group_key: str, rows: list[sqlite3.Row]) -> str:
    return sha256_text(group_key + "|" + "|".join(
        str(row["body_sha256"] or "") + (":" + str(row["phase_fingerprint"]) if "phase_fingerprint" in row.keys() and row["phase_fingerprint"] else "")
        for row in rows))


def semantic_input_sha(document: dict[str, Any]) -> str:
    """Bind queued work only to immutable mail input, not the live memory snapshot."""
    stable_document = {
        key: document[key]
        for key in ("batch_id", "mailbox_scope", "source_contract", "messages")
    }
    # Added model evidence metadata must not invalidate already queued source hashes.
    stable_document = json.loads(canonical_json(stable_document))
    for message in stable_document['messages']:
        for attachment in message.get('attachments', []):
            attachment.pop('source_text_sha256', None)
    return sha256_text(canonical_json(stable_document))


def decision_context_sha(document):
    return sha256_text(canonical_json(document))


def build_semantic_document(config: dict[str, Any], rows: list[sqlite3.Row], batch_id: str) -> tuple[dict[str, Any], str]:
    section = config["email_sync"]
    max_message = int(section["max_message_chars"])
    max_attachment = int(section["max_attachment_text_chars"])
    max_conversation = int(section["max_conversation_chars"])
    messages: list[dict[str, Any]] = []
    consumed = 0
    for index, row in enumerate(rows, 1):
        archive_root = Path(str(row["archive_path"] or ""))
        valid, reason = verify_archive(archive_root, str(row["message_key"]))
        if not valid:
            raise EmailSyncError(reason or "archive_invalid", "archived mail failed semantic revalidation")
        manifest = read_json(archive_root / "manifest.json", {})
        message = read_json(archive_root / "message.json", {})
        if not isinstance(manifest, dict) or not isinstance(message, dict):
            raise EmailSyncError("archive_payload_invalid", "archived mail payload is invalid")
        remaining = max(0, max_conversation - consumed)
        body_limit = min(max_message, remaining)
        try:
            body = (archive_root / "body.md").read_text(encoding="utf-8")
        except OSError as exc:
            raise EmailSyncError("archive_body_missing", "archived mail body is unavailable") from exc
        body_text = redact_for_model(body, body_limit)
        consumed += len(body_text)
        attachments: list[dict[str, Any]] = []
        active_policy = win11_attachments.policy(config)
        skip_self_attachments = bool(active_policy and manifest.get("attachments")
            and win11_attachments.sender_is_self(message, active_policy))
        for item in email_phases.overlay(config, row, manifest):
            if not isinstance(item, dict):
                continue
            extracted = ""
            raw_text = ""
            relative = str(item.get("extracted_text_path") or "")
            target = resolved_under(archive_root, relative) if relative else None
            full_read = None
            input_truncated = False
            if not skip_self_attachments and not item.get('ignored_by_user'):
                full_read = ({"text": item["phase_text"], "extract_status": item["extract_status"],
                              "coverage": {"complete": item["phase_complete"]}}
                             if "phase_text" in item else
                             (win11_attachments.full_read_receipt(config, item) if active_policy else None))
                raw_text = full_read["text"] if full_read else ""
                if not full_read and target and target.is_file():
                    try:
                        raw_text = target.read_text(encoding="utf-8")
                    except OSError:
                        raw_text = ""
                    receipt_relative = str(item.get("receipt_path") or "")
                    receipt_target = resolved_under(archive_root, receipt_relative) if receipt_relative else None
                    if active_policy and item.get("storage") == "win11" and receipt_target and receipt_target.is_file():
                        archived_receipt = read_json(receipt_target, {})
                        if archived_receipt.get("schema_version") == 2:
                            # verify_archive above already binds this receipt and text.
                            full_read = {**archived_receipt, "text": raw_text}
                # User approval is per original extracted file, before redaction
                # or partitioning; neither can hide an over-threshold file.
                try:
                    email_batched_input.require_file_permission(raw_text, str(item.get("original_name") or "attachment"))
                except email_batched_input.BatchInputError as exc:
                    raise EmailSyncError(exc.code, str(exc)) from exc
                # The model input budget must not masquerade as a full file read.
                # Under the all-attachment policy pass every extracted character;
                # a model context rejection remains a real failed/pending job.
                budget = len(raw_text) if active_policy else min(max_attachment, max(0, max_conversation - consumed))
                input_truncated = len(raw_text) > budget
                extracted = redact_for_model(raw_text, budget)
                if not active_policy:
                    consumed += len(extracted)
            attachments.append(
                {
                    "name": redact_for_model(str(item.get("original_name") or "附件"), 160),
                    "inline": item.get("is_inline") is True,
                    "size": normalize_int(item.get("actual_size")),
                    "risk": str(item.get("risk") or "unknown"),
                    "extract_status": ("skipped_self_sender" if skip_self_attachments
                                       else str((full_read or item).get("extract_status") or "unknown")),
                    "extracted_text": extracted,
                    "source_text_sha256": sha256_text(raw_text),
                    "attachment_input_truncated": input_truncated,
                    "full_read_confirmed": bool(full_read and full_read.get("coverage", {}).get("complete")),
                    "supplement_sha256": item.get("phase_result_sha256"),
                    "processing_complete": item.get("phase_complete", bool(full_read and full_read.get("coverage", {}).get("complete"))),
                    "video_waiting_instruction": bool(item.get('storage') == 'video_hold' or item.get('phase_video_waiting_instruction') or (full_read and video_hold.coverage_summary(full_read))),
                    "non_video_complete": item.get('phase_non_video_complete') if 'phase_non_video_complete' in item else ((video_hold.coverage_summary(full_read) or {}).get('non_video_complete') if full_read else None),
                }
            )
        messages.append(
            {
                "source_token": f"S{index:03d}",
                "direction": str(row["direction"]),
                "time": str(row["received_at"] or ""),
                "subject": redact_for_model(message.get("subject"), 240),
                "body": body_text,
                "body_input_truncated": len(body) > len(body_text),
                "attachments": attachments,
            }
        )
    try:
        open_claims = reconciliation.open_claim_context(
            "\n".join(
                " ".join((str(item.get("subject") or ""), str(item.get("body") or "")))
                for item in messages
            ),
            limit=24,
        )
    except reconciliation.ReconciliationError:
        open_claims = []
    document = {
        "batch_id": batch_id,
        "mailbox_scope": "enterprise_only",
        "source_contract": "email statements are evidence of communication, not automatically confirmed business facts",
        "messages": messages,
        "current_memory": memory.model_memory_context(runtime_paths(config)["memory_root"]),
        "open_claims": open_claims,
    }
    return document, semantic_input_sha(document)


def invoke_email_model(config: dict[str, Any], semantic_id: str, document: dict[str, Any], *, _fragment: bool = False) -> Any:
    section = config["email_sync"]
    # No codex precondition: model_router calls MiMo and Kimi directly, so a
    # missing or hung codex only removes the GPT fallback.
    if not EMAIL_PROMPT.is_file() or not EMAIL_SCHEMA.is_file():
        raise EmailSyncError("model_contract_missing", "email prompt or schema is missing")
    run_dir = runtime_paths(config)["runs"] / semantic_id / "model"
    ensure_private_dir(run_dir)
    model_document = document
    import model_router
    source_analysis_enabled = (config.get('model_routing') or {}).get('source_analysis_cache', False)
    if not _fragment and source_analysis_enabled:
        import attachment_analysis
        try:
            model_document = attachment_analysis.prepare_email(
                config, semantic_id, document, run_dir, semantic_rows(config, semantic_id))
        except email_batched_input.BatchInputError as exc:
            raise EmailSyncError(exc.code, str(exc)) from exc
        except model_router.RouterExhausted as exc:
            import model_router
            raise EmailSyncError(model_router.legacy_error_code(exc),
                                 f"email attachment model failed: {exc}") from exc
    if not _fragment and not source_analysis_enabled:
        def write_batch_json(path: Path, payload: Any) -> None:
            ensure_private_dir(path.parent)
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

        def invoke_fragment(child_id: str, child: dict[str, Any]) -> Any:
            cache_path = runtime_paths(config)["runs"] / child_id / "model" / "response.json"
            cached = read_json(cache_path, {})
            if cached:
                if cached.get("input_sha256") != semantic_input_sha(child):
                    raise EmailSyncError("batch_cache_mismatch", "cached fragment source changed")
                return cached["response"]
            return invoke_email_model(config, child_id, child, _fragment=True)

        try:
            model_document = email_batched_input.prepare(
                document, run_dir, invoke_fragment,
                lambda raw, child: validate_email_proposal(config, raw, child),
                write_batch_json, read_json, semantic_input_sha,
                sha256_text(EMAIL_PROMPT.read_text() + EMAIL_SCHEMA.read_text() + Path(email_batched_input.__file__).read_text()),
            )
        except email_batched_input.BatchInputError as exc:
            raise EmailSyncError(exc.code, str(exc)) from exc
    prompt = EMAIL_PROMPT.read_text(encoding="utf-8").rstrip() + "\n\n<email_batch>\n" + canonical_json(model_document) + "\n</email_batch>\n"
    schema = json.loads(EMAIL_SCHEMA.read_text(encoding="utf-8"))
    schema["properties"]["batch_id"]["enum"] = [str(document["batch_id"])]
    handles = sorted({str(row["claim_handle"]) for row in document.get("open_claims", [])})
    relations = schema["properties"]["claim_relations"]
    if handles:
        relations["items"]["properties"]["claim_handle"]["enum"] = handles
    else:
        relations["maxItems"] = 0
    tokens = sorted({str(row["source_token"]) for row in document["messages"]})
    def bind_tokens(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "source_tokens" and isinstance(child, dict):
                    child["items"]["enum"] = tokens
                bind_tokens(child)
        elif isinstance(value, list):
            for child in value:
                bind_tokens(child)
    bind_tokens(schema)
    schema_path = run_dir / "output-schema.json"
    atomic_write_text(schema_path, json.dumps(schema, ensure_ascii=False) + "\n")
    import model_router
    prompt += memory.mimo_schema_prompt(schema_path)
    try:
        ai_lock = memory.acquire_lock(memory.SHARED_AI_LOCK, blocking=True)
        try:
            atomic_write_text(run_dir / "invocation.json", json.dumps({
                "invocation_id": str(uuid.uuid4()), "started_at": now_iso(),
                "input_sha256": semantic_input_sha(document), "prompt_sha256": sha256_text(prompt),
            }) + "\n")
            # A reply that does not parse or fails the proposal checks is that
            # provider's failure, so the router tries the next model instead of
            # failing the mail batch here.
            def accept(text: str) -> Any:
                parsed = memory.parse_model_json(text)
                validate_email_proposal(config, parsed, document)
                return parsed

            routed = model_router.complete(config, prompt=prompt, purpose="email_semantic",
                                           timeout=int(section["model_timeout_seconds"]), workdir=run_dir,
                                           validate=accept,
                                           request_context={'scope': 'enterprise-mail',
                                               'volatile_values': {'batch_id': str(document['batch_id'])},
                                               'contract': sha256_text(EMAIL_PROMPT.read_text() + EMAIL_SCHEMA.read_text())})
        finally:
            ai_lock.close()
    except model_router.RouterExhausted as exc:
        raise EmailSyncError(model_router.legacy_error_code(exc), f"email semantic model failed: {exc}") from exc
    raw = routed.value
    # Preserve paid output before semantic validation, including rejected output.
    # A later validator repair can recheck it without another model call.
    atomic_write_text(run_dir / "response.json", json.dumps({
        "schema_version": 1, "input_sha256": semantic_input_sha(document),
        "prompt_sha256": sha256_text(prompt), "decision_context_sha256": decision_context_sha(document),
        "received_at": now_iso(), "response": raw,
    }, ensure_ascii=False) + "\n")
    return raw


def validate_email_proposal(config: dict[str, Any], payload: Any, document: dict[str, Any]) -> dict[str, Any]:
    batch_id = str(document["batch_id"])
    if not isinstance(payload, dict) or payload.get("batch_id") != batch_id:
        raise EmailSyncError("proposal_identity", "email proposal identity did not match")
    allowed_tokens = {str(item["source_token"]) for item in document["messages"]}
    records = memory.memory_records(runtime_paths(config)["memory_root"])
    by_id = {str(record["memory_id"]): record for record in records}

    source_summary = payload.get("source_summary")
    if source_summary is not None:
        if not isinstance(source_summary, dict):
            raise EmailSyncError("proposal_shape", "email source summary is invalid")
        source_summary = {
            "title": memory.sanitize_note_text(source_summary.get("title"), 100),
            "summary": memory.sanitize_note_text(source_summary.get("summary"), 1600),
            "evidence_boundary": memory.sanitize_note_text(source_summary.get("evidence_boundary"), 1000),
            "source_tokens": [str(value) for value in source_summary.get("source_tokens") or []],
        }
        if not all(source_summary[key] for key in ("title", "summary", "evidence_boundary")):
            raise EmailSyncError("proposal_empty", "email source summary contains an empty field")
        if any(contains_private_value(source_summary[key]) for key in ("title", "summary", "evidence_boundary")):
            raise EmailSyncError("proposal_privacy", "email source summary contains a private value")
        if not source_summary["source_tokens"] or not set(source_summary["source_tokens"]).issubset(allowed_tokens):
            raise EmailSyncError("proposal_source", "email source summary references an unknown token")

    allowed_actions = {"annotate_page", "link_existing", "conflict"}
    allowed_categories = {"business_cognition", "internal_material", "external_communication"}
    allowed_kinds = {"fact", "judgment", "decision", "status", "open_question"}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) > 20:
        raise EmailSyncError("proposal_shape", "email candidates are invalid")
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            raise EmailSyncError("proposal_shape", "email candidate is invalid")
        action = str(raw.get("action") or "")
        category = str(raw.get("category") or "")
        memory_kind = str(raw.get("memory_kind") or "")
        tokens = [str(value) for value in raw.get("source_tokens") or []]
        existing = str(raw.get("existing_memory_id") or "") or None
        if action not in allowed_actions or category not in allowed_categories or memory_kind not in allowed_kinds:
            raise EmailSyncError("proposal_enum", "email candidate contains an invalid type")
        if not tokens or not set(tokens).issubset(allowed_tokens):
            raise EmailSyncError("proposal_source", "email candidate references an unknown token")
        if action == "annotate_page" and existing is not None:
            raise EmailSyncError("proposal_reference", "new email candidate claimed an existing memory")
        if action in {"link_existing", "conflict"}:
            record = by_id.get(str(existing))
            if not record or not (record["status"] == "active" and record["fact_status"] == "confirmed"):
                raise EmailSyncError("proposal_reference", "email candidate references an ineligible memory")
        conflict_reason = memory.sanitize_note_text(raw.get("conflict_reason"), 800) if raw.get("conflict_reason") else None
        if action == "conflict" and not conflict_reason:
            raise EmailSyncError("proposal_conflict", "email conflict lacks a reason")
        if action != "conflict" and conflict_reason:
            raise EmailSyncError("proposal_conflict", "non-conflict email candidate has a conflict reason")
        candidate = {
            "action": action,
            "dedupe_key": memory.sanitize_note_text(raw.get("dedupe_key"), 120),
            "title": memory.sanitize_note_text(raw.get("title"), 100),
            "category": category,
            "memory_kind": memory_kind,
            "summary": memory.sanitize_note_text(raw.get("summary"), 1800),
            "evidence_boundary": memory.sanitize_note_text(raw.get("evidence_boundary"), 1200),
            "source_tokens": tokens,
            "existing_memory_id": existing,
            "conflict_reason": conflict_reason,
        }
        if not all(candidate[key] for key in ("dedupe_key", "title", "summary", "evidence_boundary")):
            raise EmailSyncError("proposal_empty", "email candidate contains an empty field")
        if any(
            contains_private_value(candidate.get(key))
            for key in ("dedupe_key", "title", "summary", "evidence_boundary", "conflict_reason")
        ):
            raise EmailSyncError("proposal_privacy", "email candidate contains a private value")
        if candidate["dedupe_key"] in seen:
            raise EmailSyncError("proposal_duplicate", "email proposal repeats a semantic key")
        seen.add(str(candidate["dedupe_key"]))
        candidates.append(candidate)

    project_updates: list[dict[str, Any]] = []
    raw_updates = payload.get("project_updates")
    if not isinstance(raw_updates, list) or len(raw_updates) > 8:
        raise EmailSyncError("proposal_shape", "email project updates are invalid")
    for raw in raw_updates:
        if not isinstance(raw, dict):
            raise EmailSyncError("proposal_shape", "email project update is invalid")
        tokens = [str(value) for value in raw.get("source_tokens") or []]
        status_name = str(raw.get("status") or "")
        if not tokens or not set(tokens).issubset(allowed_tokens) or status_name not in {"source_observed", "pending", "conflict"}:
            raise EmailSyncError("proposal_source", "email project update is unsupported")
        name = public_report_text(raw.get("name"), 80)
        progress = public_report_text(raw.get("progress"), 240)
        if name and progress:
            # The cached proposal is validated again before retry/apply.  Keep
            # its evidence binding; a presentation-only projection is not a
            # reusable proposal.
            project_updates.append({"name": name, "progress": progress, "status": status_name,
                                    "source_tokens": tokens})

    no_change = payload.get("no_business_change") is True
    if no_change != (source_summary is None and not candidates and not project_updates):
        raise EmailSyncError("proposal_shape", "email no-business flag is inconsistent")
    if not no_change and source_summary is None:
        raise EmailSyncError("proposal_shape", "business email proposal requires a source summary")
    raw_relations = payload.get("claim_relations") or []
    try:
        relations = reconciliation.validate_relations(
            raw_relations,
            allowed_handles=reconciliation.open_claim_handles() if raw_relations else set(),
            allowed_tokens=allowed_tokens,
        )
    except reconciliation.ReconciliationError as exc:
        raise EmailSyncError("proposal_claim_relation", exc.message) from exc
    return {
        "batch_id": batch_id,
        "source_summary": source_summary,
        "candidates": candidates,
        "project_updates": project_updates,
        "claim_relations": relations,
        "no_business_change": no_change,
    }


def source_note_change(
    config: dict[str, Any],
    records: list[dict[str, Any]],
    reserved: set[str],
    source_ref: str,
    group_key: str,
    proposal: dict[str, Any],
    observed_at: str,
) -> tuple[Path, str, str]:
    memory_root = runtime_paths(config)["memory_root"]
    from sync_rule_runtime import email_source
    existing = [record for record in records if email_source(record, source_ref)]
    if len(existing) > 1:
        raise EmailSyncError("source_note_collision", "multiple email source notes share one reference")
    summary = proposal["source_summary"]
    stamp = now_iso()
    marker = f"<!-- email-source:{group_key} -->"
    body = (
        f"{marker}\n\n## 来源摘要\n\n{summary['summary']}\n\n"
        f"## 证据边界\n\n{summary['evidence_boundary']} 邮件只证明相关表述曾出现，未自动升级为公司确认事实。\n\n"
        f"## 来源\n\n- `{source_ref}`；原始邮件、附件、标识和模型中间文件只保存在本机 `.nosync` 来源库。\n\n"
        f"## 更新记录\n\n- {stamp}：完成企业邮件会话的来源索引与语义提炼。\n"
    )
    if existing:
        record = existing[0]
        text = memory.rebuild_existing_note(
            str(record["raw_frontmatter"]), body,
            {"status": "active", "evidence_level": "internal_source", "source_check": "matched", "observed_at": observed_at, "processed_at": stamp},
        )
        return Path(record["path"]), text, str(record["memory_id"])
    date_key = observed_at[:10].replace("-", "") if len(observed_at) >= 10 else datetime.now().strftime("%Y%m%d")
    memory_id = memory.next_memory_id(
        records,
        date_key,
        reserved,
        vault_root=memory_root.parent,
    )
    reserved.add(memory_id)
    title = memory.safe_filename(f"企业邮件-{summary['title']}", 62)
    path = memory_root / "03_外部沟通" / f"{date_key}-{title}.md"
    if path.exists():
        path = memory_root / "03_外部沟通" / f"{date_key}-{title[:48]}-{group_key[:8]}.md"
    frontmatter = [
        ("memory_id", memory_id), ("category", "external_communication"),
        ("memory_scope", "internal_nonpublic"), ("memory_kind", "source_index"),
        ("origin_type", "mixed"), ("status", "active"),
        ("evidence_level", "internal_source"), ("sensitivity", "confidential"),
        ("source_system", "email"), ("source_check", "matched"),
        ("fact_status", "not_applicable"), ("source_ref", source_ref),
        ("observed_at", observed_at), ("processed_at", stamp), ("reviewed_at", None),
        ("valid_until", None), ("supersedes", []), ("content_hash", ""),
        ("applies_to", ["project:business-energy"]),
    ]
    return path, memory.render_note(frontmatter, body), memory_id


def pending_note_change(
    config: dict[str, Any],
    records: list[dict[str, Any]],
    reserved: set[str],
    source_ref: str,
    candidate: dict[str, Any],
    observed_at: str,
    page_overrides: Optional[dict[Path, str]] = None,
) -> tuple[Path, str, str]:
    del records, reserved
    enriched = {
        **candidate,
        "evidence_boundary": (
            f"{candidate['evidence_boundary']} 该内容来自单一邮件会话，仍需责任人或权威原件确认。"
        ),
        "evidence_state": "conflicting" if candidate["action"] == "conflict" else "pending",
    }
    try:
        path, text, route = business_router.route_claim_change(
            config,
            enriched,
            source_system="email",
            source_ref=source_ref,
            observed_at=observed_at,
            page_overrides=page_overrides,
        )
    except business_router.RoutingError as exc:
        quarantine = business_router.quarantine_candidate(
            config,
            {**enriched, "source_system": "email", "source_ref": source_ref},
            exc.code,
        )
        raise EmailSyncError(
            "business_routing_failed",
            f"email candidate was quarantined after routing failure: {quarantine.name}",
        ) from exc
    return path, text, route.claim_id


def _mail_source_target(memory_root, path):
    indexes = {memory_root / '00_记忆总览.md', memory_root / '99_规则与模板/AI检索总表.md', memory_root / '90_待确认/00_待确认总表.md'}
    return path not in indexes and memory.safe_target(memory_root, path)


def _rollback_mail_journal(config, journal_path, journal):
    memory_root = runtime_paths(config)["memory_root"].resolve()
    if journal.get('schema_version') == 2 and journal.get('state') == 'restored_pending_validation':
        # Sources were already restored under lock. Rebuild from their current
        # legitimate state, without rolling later updates back a second time.
        memory.run_refresh_and_validate(config, lock_held=True, attempts=1)
        return True
    result = mail_transaction_recovery.rollback_transaction(
        journal, backup_root=(journal_path.parent / "backup").resolve(),
        safe_target=lambda path: _mail_source_target(memory_root, path), lock_held=True,
    )
    if result["status"] == "conflict":
        # Never replace a later writer's files or reinterpret a legacy snapshot.
        evidence = {"schema_version": 1, "error_code": "recovery_source_conflict",
                    "codes": sorted({x["code"] for x in result["conflicts"]}), "at": now_iso()}
        atomic_write_text(journal_path.with_name("recovery-conflict.json"), json.dumps(evidence) + "\n")
        mail_auto_recovery.observe(config, str(journal.get("semantic_id") or "unknown"),
            "transaction", sha256_text(canonical_json(journal)), "recovery_source_conflict")
        raise EmailSyncError("recovery_source_conflict", "A later or unproven write was preserved")
    if result["status"] == "noop": return False
    journal["state"] = "restored_pending_validation"
    journal["recovered_at"] = now_iso()
    atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    # Derived indexes are rebuilt from present source files, never old snapshots.
    memory.run_refresh_and_validate(config, lock_held=True, attempts=1)
    return True


def write_memory_transaction(
    config: dict[str, Any], semantic_id: str, changes: dict[Path, str], *, lock_held: bool = False
) -> dict[str, Any]:
    if not changes: return {"writes": 0, "validated": False}
    lock = None if lock_held else memory.acquire_lock(memory.MEMORY_LOCK, blocking=False)
    try:
        if maintenance_active():
            raise EmailSyncError("maintenance_active", "business memory is in maintenance mode")
        memory_root = runtime_paths(config)["memory_root"].resolve()
        run_dir = runtime_paths(config)["runs"] / semantic_id / "memory-transaction"
        backup_root = (run_dir / "backup").resolve()
        ensure_private_dir(backup_root)
        journal_path = run_dir / "transaction.json"
        existing = read_json(journal_path, {})
        if existing and existing.get('state') not in {'rolled_back','rolled_back_after_recovery'}:
            raise EmailSyncError('recovery_transaction_unfinished', 'Existing transaction requires evidence reconciliation')
        entries = []
        for original, text in changes.items():
            if original.is_symlink() or not memory.safe_target(memory_root, original):
                raise EmailSyncError("unsafe_memory_target", "email target is outside the allowlist")
            target = original.resolve()
            backup = None
            if target.exists():
                if not target.is_file(): raise EmailSyncError('unsafe_memory_target','Target is not a regular file')
                backup = backup_root / (sha256_text(str(target)) + '.txt')
                shutil.copyfile(target, backup); backup.chmod(0o600)
            entries.append(mail_transaction_recovery.make_entry(
                target, str(text).encode('utf-8'), backup=backup, backup_root=backup_root,
                safe_target=lambda path: _mail_source_target(memory_root,path)))
        journal = {"schema_version": 2, "semantic_id": semantic_id, "state": "prepared",
                   "created_at": now_iso(), "paths": entries,
                   "derived_indexes": ["00_记忆总览.md","99_规则与模板/AI检索总表.md","90_待确认/00_待确认总表.md"]}
        atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        try:
            journal["state"] = "applying"
            atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            for target, text in changes.items():
                if isinstance(text, business_router.RoutedText):
                    try: business_router.verify_expected_hash(target, text)
                    except business_router.RoutingError as exc: raise EmailSyncError(exc.code, exc.message) from exc
                atomic_write_text(target, text, mode=stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644)
            verification = memory.run_refresh_and_validate(config, lock_held=True)
            journal["state"] = "committed"; journal["committed_at"] = now_iso()
            atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            return {"writes": len(changes), "validated": True, "verification": verification}
        except Exception:
            _rollback_mail_journal(config, journal_path, journal)
            journal["state"] = "rolled_back"; journal["rolled_back_at"] = now_iso()
            atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            raise
    finally:
        if lock is not None: lock.close()


def recover_memory_transactions(config: dict[str, Any]) -> int:
    runs_root = runtime_paths(config)["runs"]
    if not runs_root.is_dir() or runs_root.is_symlink(): return 0
    journals = []
    for path in sorted(runs_root.glob("*/memory-transaction/transaction.json")):
        payload = read_json(path, {})
        if isinstance(payload, dict) and payload.get('state') in {'prepared','applying','restored_pending_validation',mail_legacy_recovery.STATE}:
            journals.append((path, payload))
    if not journals: return 0
    if maintenance_active(): raise EmailSyncError('maintenance_active','Cannot recover while maintenance is active')
    try: lock=memory.acquire_lock(memory.MEMORY_LOCK,blocking=False)
    except memory.AutomationError as exc: raise EmailSyncError('memory_lock_busy','Memory writer is busy') from exc
    recovered=0
    try:
        for path,journal in journals:
            if journal.get('schema_version') == 1:
                try:
                    mail_legacy_recovery.reconcile(config, path, journal, lock_held=True)
                except (ValueError, OSError, KeyError):
                    pass  # Unproven history retains the normal recovery boundary.
                else:
                    recovered += int(journal.get('state') != mail_legacy_recovery.STATE)
                    continue
            try: restored=_rollback_mail_journal(config,path,journal)
            except EmailSyncError as exc:
                if exc.code=='recovery_source_conflict': continue
                raise
            if restored:
                journal['state']='rolled_back_after_recovery';journal['recovery_validated_at']=now_iso()
                atomic_write_text(path,json.dumps(journal,ensure_ascii=False,indent=2,sort_keys=True)+'\n')
                mail_auto_recovery.recovered(config,str(journal.get('semantic_id')),'transaction',{'rollback_validated':True})
                recovered+=1
        return recovered
    finally: lock.close()


def apply_email_proposal(
    config: dict[str, Any],
    semantic_id: str,
    group_key: str,
    source_ref: str,
    rows: list[sqlite3.Row],
    proposal: dict[str, Any],
    expected_context_sha: Optional[str] = None,
) -> dict[str, Any]:
    email_phases.assert_current(config, rows)
    if proposal["no_business_change"] and expected_context_sha is None:
        return {"writes": 0, "source_summary_written": 0, "pending_candidates": 0, "linked_existing": 0, "conflicts": 0}
    deadline = time.monotonic() + 45
    while True:
        try:
            lock = memory.acquire_lock(memory.MEMORY_LOCK, blocking=False)
            break
        except memory.AutomationError as exc:
            if exc.code != "already_running" or time.monotonic() >= deadline:
                raise
            time.sleep(1)
    try:
        if maintenance_active():
            raise EmailSyncError("maintenance_active", "business memory is in maintenance mode")
        if expected_context_sha is not None:
            fresh, _ = build_semantic_document(config, semantic_rows(config, semantic_id), semantic_id)
            if decision_context_sha(fresh) != expected_context_sha:
                raise EmailSyncError('model_context_changed', 'Source or business context changed before commit')
            if proposal['no_business_change']:
                return {"writes":0,"source_summary_written":0,"pending_candidates":0,"linked_existing":0,"conflicts":0}
        return apply_email_proposal_locked(
            config, semantic_id, group_key, source_ref, rows, proposal
        )
    finally:
        lock.close()


def apply_email_proposal_locked(
    config: dict[str, Any],
    semantic_id: str,
    group_key: str,
    source_ref: str,
    rows: list[sqlite3.Row],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    email_phases.assert_current(config, rows)
    memory_root = runtime_paths(config)["memory_root"]
    records = memory.memory_records(memory_root)
    by_id = {str(record["memory_id"]): record for record in records}
    for candidate in proposal["candidates"]:
        if candidate["action"] not in {"link_existing", "conflict"}:
            continue
        target = by_id.get(str(candidate["existing_memory_id"] or ""))
        if not target or not (
            target["status"] == "active" and target["fact_status"] == "confirmed"
        ):
            raise EmailSyncError(
                "proposal_reference_changed",
                "email proposal reference changed before the public lock was acquired",
            )
    observed_at = max((str(row["received_at"] or "") for row in rows), default=now_iso())
    reserved: set[str] = set()
    source_path, source_text, _source_id = source_note_change(
        config, records, reserved, source_ref, group_key, proposal, observed_at
    )
    changes: dict[Path, str] = {source_path: source_text}
    pending = 0
    linked = 0
    conflicts = 0
    linked_bodies: dict[str, str] = {}
    for candidate in proposal["candidates"]:
        if candidate["action"] == "link_existing":
            target = by_id[str(candidate["existing_memory_id"])]
            marker = f"<!-- {source_ref}:{sha256_text(candidate['dedupe_key'])[:16]} -->"
            entry = f"{marker}\n- {now_iso()}｜{candidate['summary']}｜来源：`{source_ref}`。未改变原条目的事实状态。"
            target_id = str(target["memory_id"])
            body = memory.append_section_entry(
                linked_bodies.get(target_id, str(target["body"])), "邮件来源补充", entry, marker
            )
            linked_bodies[target_id] = body
            changes[Path(target["path"])] = memory.rebuild_existing_note(str(target["raw_frontmatter"]), body, {})
            linked += 1
        else:
            path, text, _memory_id = pending_note_change(
                config, records, reserved, source_ref, candidate, observed_at, changes
            )
            if path in changes and not isinstance(text, business_router.RoutedText):
                raise EmailSyncError("candidate_path_collision", "email pending-note path is not unique")
            changes[path] = text
            pending += 1
            conflicts += int(candidate["action"] == "conflict")
    result = write_memory_transaction(config, semantic_id, changes, lock_held=True)
    return {
        **result,
        "source_summary_written": 1,
        "pending_candidates": pending,
        "linked_existing": linked,
        "conflicts": conflicts,
    }


def mark_semantic_queue_failure(
    config: dict[str, Any], rows: list[sqlite3.Row], code: str
) -> None:
    keys = [str(row["message_key"]) for row in rows]
    if not keys:
        return
    placeholders = ",".join("?" for _ in keys)
    connection = connect_database(config)
    try:
        connection.execute(
            f"""
            UPDATE messages SET semantic_attempts=semantic_attempts+1,last_error_code=?,updated_at=?
            WHERE message_key IN ({placeholders}) AND memory_state='pending'
            """,
            (code[:80], now_iso(), *keys),
        )
        connection.commit()
    finally:
        connection.close()


def prepare_semantic_group(
    config: dict[str, Any], batch_id: str, group_key: str, trigger_rows: list[sqlite3.Row]
) -> int:
    all_rows = full_conversation_rows(config, trigger_rows[0])
    semantic_key = semantic_key_for_rows(group_key, all_rows)
    semantic_id = str(uuid.uuid4())
    document, input_sha = build_semantic_document(config, all_rows, semantic_id)
    del document
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT semantic_id FROM semantic_groups WHERE semantic_key=?", (semantic_key,)
        ).fetchone()
        if existing:
            connection.commit()
            return 0
        source_ref = f"email-conversation:{group_key}"
        connection.execute(
            """
            INSERT INTO semantic_groups(
              semantic_id,semantic_key,group_key,source_ref,batch_id,input_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (semantic_id, semantic_key, group_key, source_ref, batch_id, input_sha, now_iso()),
        )
        for index, row in enumerate(all_rows, 1):
            connection.execute(
                "INSERT INTO semantic_group_messages(semantic_id,message_key,source_token) VALUES(?,?,?)",
                (semantic_id, str(row["message_key"]), f"S{index:03d}"),
            )
        connection.commit()
        return 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_semantic_groups(config: dict[str, Any], batch_id: str) -> int:
    connection = connect_database(config)
    try:
        pending_rows = connection.execute(
            """
            SELECT m.* FROM messages m JOIN batch_messages bm ON bm.message_key=m.message_key
            WHERE bm.batch_id=? AND m.archive_state='archived' AND m.memory_state='pending'
            ORDER BY m.received_at,m.message_key
            """,
            (batch_id,),
        ).fetchall()
    finally:
        connection.close()
    if not pending_rows:
        return 0
    mailbox = str(config["email_sync"]["mailbox"])
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in pending_rows:
        key = conversation_key(mailbox, str(row["conversation_id"] or ""), str(row["message_key"]))
        groups.setdefault(key, []).append(row)
    created = 0
    for group_key, trigger_rows in groups.items():
        try:
            created += prepare_semantic_group(config, batch_id, group_key, trigger_rows)
        except (EmailSyncError, memory.AutomationError, OSError, ValueError) as exc:
            mark_semantic_queue_failure(
                config, trigger_rows, getattr(exc, "code", type(exc).__name__)
            )
    return created


def semantic_rows(config: dict[str, Any], semantic_id: str) -> list[sqlite3.Row]:
    connection = connect_database(config)
    try:
        return connection.execute(
            """
            SELECT m.* FROM messages m JOIN semantic_group_messages sgm ON sgm.message_key=m.message_key
            WHERE sgm.semantic_id=? ORDER BY sgm.source_token
            """,
            (semantic_id,),
        ).fetchall()
    finally:
        connection.close()


def rebase_legacy_semantic_input_if_safe(
    config: dict[str, Any],
    group: sqlite3.Row,
    rows: list[sqlite3.Row],
    input_sha: str,
    proposal_path: Path,
) -> bool:
    """Migrate only the known pre-fix failure when its archived source is unchanged."""
    if (
        str(group["status"]) != "failed"
        or str(group["last_error_code"] or "") != "semantic_input_changed"
        or bool(group["proposal_path"])
        or proposal_path.parent.exists()
        or semantic_key_for_rows(str(group["group_key"]), rows) != str(group["semantic_key"])
    ):
        return False
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE semantic_groups SET input_sha256=?
            WHERE semantic_id=? AND status='failed' AND last_error_code='semantic_input_changed'
              AND proposal_path IS NULL AND semantic_key=? AND input_sha256=?
            """,
            (
                input_sha,
                str(group["semantic_id"]),
                str(group["semantic_key"]),
                str(group["input_sha256"]),
            ),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def supersede_expanded_legacy_semantic_groups(config: dict[str, Any]) -> int:
    """Retire source-stable legacy failures when newly archived mail completes the conversation."""
    connection = connect_database(config)
    try:
        groups = connection.execute(
            """
            SELECT * FROM semantic_groups
            WHERE status='failed' AND last_error_code='semantic_input_changed'
              AND proposal_path IS NULL
            ORDER BY created_at
            """
        ).fetchall()
    finally:
        connection.close()
    superseded = 0
    for group in groups:
        semantic_id = str(group["semantic_id"])
        if (runtime_paths(config)["runs"] / semantic_id).exists():
            continue
        rows = semantic_rows(config, semantic_id)
        if not rows or semantic_key_for_rows(str(group["group_key"]), rows) != str(group["semantic_key"]):
            continue
        full_rows = full_conversation_rows(config, rows[0])
        if not all(
            verify_archive(Path(str(row["archive_path"])), str(row["message_key"]))[0]
            for row in full_rows
        ):
            continue
        stored_keys = {str(row["message_key"]) for row in rows}
        full_keys = {str(row["message_key"]) for row in full_rows}
        if not stored_keys < full_keys:
            continue
        connection = connect_database(config)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE semantic_groups
                SET status='superseded',last_error_code='conversation_expanded_before_processing',
                  completed_at=?
                WHERE semantic_id=? AND status='failed' AND last_error_code='semantic_input_changed'
                  AND proposal_path IS NULL AND semantic_key=? AND input_sha256=?
                """,
                (
                    now_iso(),
                    semantic_id,
                    str(group["semantic_key"]),
                    str(group["input_sha256"]),
                ),
            )
            connection.commit()
            superseded += int(cursor.rowcount == 1)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    return superseded


def process_semantic_group(config: dict[str, Any], group: sqlite3.Row) -> dict[str, Any]:
    semantic_id = str(group["semantic_id"])
    minutes_handoff = {
        "status": "not_needed",
        "processed": 0,
        "done": 0,
        "deferred": 0,
        "failed": 0,
    }
    try:
        return _process_semantic_group(
            config,
            group,
            semantic_id=semantic_id,
            handoff_receipt=minutes_handoff,
        )
    finally:
        if minutes_handoff.get("status") == "pending_after_model_call":
            minutes_handoff.clear()
            minutes_handoff.update(syncv2.run_minutes_review_handoff())


def _process_semantic_group(
    config: dict[str, Any],
    group: sqlite3.Row,
    *,
    semantic_id: str,
    handoff_receipt: dict[str, Any],
) -> dict[str, Any]:
    rows = semantic_rows(config, semantic_id)
    document, input_sha = build_semantic_document(config, rows, semantic_id)
    proposal_path = runtime_paths(config)["runs"] / semantic_id / "proposal.json"
    if input_sha != str(group["input_sha256"]) and not rebase_legacy_semantic_input_if_safe(
        config, group, rows, input_sha, proposal_path
    ):
        raise EmailSyncError("semantic_input_changed", "email semantic input changed after queueing")
    guarded = bool((config.get('model_routing') or {}).get('source_analysis_cache', False))
    context_sha = decision_context_sha(document)
    metadata_path = proposal_path.with_name('proposal-context.json')
    metadata = read_json(metadata_path, {})
    if guarded and proposal_path.is_file() and metadata.get('decision_context_sha256') != context_sha:
        archive = proposal_path.with_name('proposal.previous-' + sha256_file(proposal_path) + '.json')
        if not archive.exists(): atomic_write_text(archive, proposal_path.read_text())
        # Preserve the old proposal until a new one has completed; don't use it for this input.
    reusable_proposal = proposal_path.is_file() and (not guarded or metadata.get('decision_context_sha256') == context_sha)
    if reusable_proposal:
        proposal = validate_email_proposal(config, read_json(proposal_path, {}), document)
        model_called = False
    else:
        cache_path = proposal_path.parent / "model" / "response.json"
        cached = read_json(cache_path, {})
        proposal = None
        model_called = False
        if cached and (not guarded or cached.get('decision_context_sha256') == context_sha):
            if cached.get("input_sha256") != input_sha:
                raise EmailSyncError("semantic_input_changed", "cached model output has different source input")
            try:
                proposal = validate_email_proposal(config, cached["response"], document)
            except EmailSyncError:
                # A kept answer that fails the checks fails them on every retry,
                # and the model was never asked again: two mails sat failed for
                # days (2020-01-02). Keep it as evidence and ask the chain anew.
                os.replace(cache_path, cache_path.with_name(f"response.rejected-{int(time.time())}.json"))
        if proposal is None:
            handoff_receipt["status"] = "pending_after_model_call"
            raw = invoke_email_model(config, semantic_id, document)
            model_called = True
            proposal = validate_email_proposal(config, raw, document)
        atomic_write_text(proposal_path, json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if guarded:
            atomic_write_text(metadata_path, json.dumps({'decision_context_sha256':context_sha}) + '\n')
    result = apply_email_proposal(
        config,
        semantic_id,
        str(group["group_key"]),
        str(group["source_ref"]),
        rows,
        proposal,
        **({'expected_context_sha':context_sha} if guarded else {}),
    )
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE semantic_groups SET status='applied',proposal_path=?,source_summary_written=?,memory_writes=?,
              pending_candidates=?,linked_existing=?,conflicts=?,project_updates_json=?,attempts=attempts+1,
              last_error_code=NULL,completed_at=? WHERE semantic_id=?
            """,
            (
                str(proposal_path), int(result.get("source_summary_written") or 0), int(result.get("writes") or 0),
                int(result.get("pending_candidates") or 0), int(result.get("linked_existing") or 0),
                int(result.get("conflicts") or 0), json.dumps(proposal["project_updates"], ensure_ascii=False),
                now_iso(), semantic_id,
            ),
        )
        for row_index, row in enumerate(rows):
            for item in document["messages"][row_index].get("attachments", []):
                if item.get("supplement_sha256"):
                    connection.execute("UPDATE attachment_jobs SET state='complete' WHERE message_key=? AND state='ready' AND result_sha256=?", (str(row["message_key"]), item["supplement_sha256"]))
            connection.execute(
                "UPDATE messages SET memory_state='applied',memory_applied_at=?,body_memory_applied_at=COALESCE(body_memory_applied_at,?),phase_applied_fingerprint=phase_fingerprint,last_error_code=NULL,updated_at=? WHERE message_key=?",
                (now_iso(), now_iso(), now_iso(), str(row["message_key"])),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    source_material = {
        str(item["source_token"]): {
            "material": " ".join((str(item.get("subject") or ""), str(item.get("body") or ""))),
            "directness": "direct",
            "verification_method": "model_proposed",
        }
        for item in document["messages"]
    }
    relation_receipt = reconciliation.submit_relations(
        config,
        list(proposal.get("claim_relations") or []),
        source_system="email",
        source_material=source_material,
        observed_at=max(
            (str(item.get("time") or "") for item in document["messages"]),
            default=now_iso(),
        ),
    )
    try:
        refresh_receipt: dict[str, Any] = reconciliation.refresh_index(
            config,
            run_id=f"email-refresh-{semantic_id}-{uuid.uuid4().hex[:8]}",
        )
    except (OSError, sqlite3.Error, reconciliation.ReconciliationError, business_router.RoutingError) as exc:
        refresh_receipt = {
            "status": "deferred",
            "error_code": getattr(exc, "code", type(exc).__name__),
        }
    return {
        "model_called": model_called,
        "minutes_handoff": handoff_receipt,
        "claim_relations": relation_receipt,
        "reconciliation": refresh_receipt,
        **result,
    }


def mark_semantic_failure(config: dict[str, Any], semantic_id: str, code: str) -> None:
    connection = connect_database(config)
    try:
        connection.execute(
            "UPDATE semantic_groups SET status='failed',attempts=attempts+1,last_error_code=? WHERE semantic_id=?",
            (code, semantic_id),
        )
        connection.execute(
            """
            UPDATE messages SET semantic_attempts=semantic_attempts+1,last_error_code=?,updated_at=?
            WHERE message_key IN (SELECT message_key FROM semantic_group_messages WHERE semantic_id=?)
              AND memory_state='pending'
            """,
            (code, now_iso(), semantic_id),
        )
        connection.commit()
    finally:
        connection.close()


def semantic_retry_key(config: dict[str, Any], group: sqlite3.Row) -> str:
    proposal = runtime_paths(config)["runs"] / str(group["semantic_id"]) / "proposal.json"
    receipt = read_json(BRIDGE_ROOT / "state/last-memory-validation-success.json", {})
    recovery = receipt.get("validation_context") if group["last_error_code"] == "validation_failed" else None
    return sha256_text(canonical_json({
        "input": group["input_sha256"], "implementation": sha256_file(Path(__file__)),
        "batch_implementation": sha256_file(Path(email_batched_input.__file__)),
        "prompt": sha256_file(EMAIL_PROMPT), "schema": sha256_file(EMAIL_SCHEMA),
        "proposal": sha256_file(proposal) if proposal.is_file() else None, "core_recovery": recovery,
    }))


def process_pending_semantics(config: dict[str, Any], max_calls: Optional[int] = None, *, exclude_ids: Iterable[str] = ()) -> dict[str, int]:
    limit = max_calls or int(config["email_sync"]["max_model_calls_per_run"])
    connection = connect_database(config)
    try:
        groups = connection.execute(
            "SELECT * FROM semantic_groups WHERE status IN ('pending','failed') ORDER BY created_at",
        ).fetchall()
    finally:
        connection.close()
    applied = 0
    failed = 0
    model_calls = 0
    writes = 0
    handoff_attempted = 0
    handoff_completed = 0
    handoff_deferred = 0
    handoff_failed = 0
    suppressed = 0
    idle_deferred = 0
    actual_attempted = 0
    recovery_probes = 0
    for index, group in enumerate(groups):
        if actual_attempted >= limit: break
        with connect_database_readonly(config) as hold_db:
            if hold_db.execute('SELECT 1 FROM meta WHERE key=?',('semantic-owner-defer:'+str(group['group_key']),)).fetchone():
                suppressed += 1
                continue
        if group["semantic_id"] in exclude_ids:
            suppressed += 1
            continue
        if group['status'] == 'failed' and group['last_error_code']:
            recovery_task = mail_auto_recovery.observe(config, str(group['semantic_id']), 'semantic', str(group['input_sha256']), group['last_error_code'])
            if recovery_task:
                state = recovery_task['state']; due = mail_auto_recovery.parse(recovery_task['next_due'])
                allowed = state not in ('needs_user','needs_maintenance') and (not due or due <= mail_auto_recovery.now())
                if allowed and state == 'waiting_probe':
                    rows = semantic_rows(config, str(group['semantic_id']))
                    allowed = bool(rows) and recovery_probes < 3
                    if allowed:
                        recovery_probes += 1
                        allowed = mail_auto_recovery.grant_probe(config, recovery_task, rows[0])
                if not allowed:
                    failed += 1; suppressed += 1
                    continue
        retry_path = runtime_paths(config)["runs"] / str(group["semantic_id"]) / "semantic-retry.json"
        invocation_path = retry_path.parent / "model" / "invocation.json"
        prior_invocation = read_json(invocation_path, {}).get("invocation_id")
        retry_key = semantic_retry_key(config, group)
        prior = read_json(retry_path, {})
        deterministic = str(prior.get("error_code", "")).startswith(("proposal_", "semantic_input_", "validation_", "privacy", "source_", "batch_", "attachment_processing_approval_required"))
        interrupted_without_cache = (prior.get("status") == "running"
                                     and not ((config.get("model_routing") or {}).get("persistent_guard", False) and prior.get("persistent_guard") is True)
                                     and not retry_path.with_name("proposal.json").is_file()
                                     and not (retry_path.parent / "model" / "response.json").is_file())
        if prior.get("input_key") == retry_key and (interrupted_without_cache or (prior.get("status") == "failed" and deterministic)):
            # Neither an unchanged failure nor an interrupted attempt is a new
            # model request. Keep the original error visible until inputs or
            # the relevant repair change.
            mail_auto_recovery.park(config, str(group['semantic_id']), 'semantic')
            failed += 1
            suppressed += 1
            continue
        actual_attempted += 1
        try:
            ensure_private_dir(retry_path.parent)
            atomic_write_text(retry_path, json.dumps({"schema_version": 1, "status": "running",
                                                     "persistent_guard": bool((config.get("model_routing") or {}).get("persistent_guard", False)),
                                                     "input_key": retry_key, "started_at": now_iso()}) + "\n")
            result = process_semantic_group(config, group)
            atomic_write_text(retry_path, json.dumps({"schema_version": 1, "status": "applied",
                                                     "input_key": retry_key, "completed_at": now_iso()}) + "\n")
            mail_auto_recovery.recovered(config, str(group["semantic_id"]), "semantic", {"semantic_applied": True, "input_sha256": group["input_sha256"]})
            applied += 1
            writes += int(result.get("writes") or 0)
            handoff = result.get("minutes_handoff") if isinstance(result.get("minutes_handoff"), dict) else {}
            if result.get("model_called") is True:
                handoff_attempted += 1
                handoff_completed += int(handoff.get("done") or 0)
                handoff_deferred += int(handoff.get("deferred") or 0)
                handoff_failed += int(handoff.get("status") == "failed")
        except (EmailSyncError, memory.AutomationError, OSError, sqlite3.Error, ValueError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            mark_semantic_failure(config, str(group["semantic_id"]), code)
            mail_auto_recovery.observe(config, str(group["semantic_id"]), "semantic", str(group["input_sha256"]), code, attempted=True)
            failed_group = dict(group)
            failed_group["last_error_code"] = code
            atomic_write_text(retry_path, json.dumps({"schema_version": 1, "status": "failed",
                                                     "input_key": semantic_retry_key(config, failed_group),
                                                     "error_code": code, "detail": str(exc)[:500],
                                                     "completed_at": now_iso()}) + "\n")
            failed += 1
        finally:
            current_invocation = read_json(invocation_path, {}).get("invocation_id")
            model_calls += int(bool(current_invocation) and current_invocation != prior_invocation)
    return {
        "attempted": actual_attempted,
        "idle_deferred": idle_deferred,
        "suppressed_unchanged": suppressed,
        "applied": applied,
        "failed": failed,
        "model_calls": model_calls,
        "memory_writes": writes,
        "handoff_attempted": handoff_attempted,
        "handoff_completed": handoff_completed,
        "handoff_deferred": handoff_deferred,
        "handoff_failed": handoff_failed,
    }


DELIVERY_RETRY_DELAY = timedelta(minutes=15)
MAX_DELIVERY_ATTEMPTS = 5
MAX_OUTBOX_EVENTS_PER_RUN = 10
DIRECTORY_MAX_AGE = timedelta(minutes=15)
TARGET_VALIDATION_MAX_AGE = timedelta(days=30)
RETRYABLE_DELIVERY_ERRORS = {
    "credentials_unavailable",
    "token_request_failed",
    "token_unavailable",
    "feishu_rejected",
    "target_map_missing",
    "target_map_unverified",
    "target_validation_stale",
    "target_alias_missing",
    "target_alias_permissions",
    "channel_directory_stale",
    "channel_directory_target_missing",
    "group_allowlist_missing",
    "default_group_policy_not_closed",
    "gateway_not_running",
    "live_target_lookup_failed",
    "live_target_mismatch",
}


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def file_mode_is_0600(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600
    except OSError:
        return False


def profile_paths(config: dict[str, Any]) -> dict[str, Path]:
    root = runtime_paths(config)["profile_root"]
    return {
        "root": root,
        "config": root / "config.yaml",
        "aliases": root / "channel_aliases.json",
        "directory": root / "channel_directory.json",
    }


def directory_entry(config: dict[str, Any], target_id: str) -> Optional[dict[str, Any]]:
    paths = profile_paths(config)
    directory = read_json(paths["directory"], {})
    entries = (directory.get("platforms") or {}).get("feishu") if isinstance(directory, dict) else None
    if not isinstance(entries, list):
        return None
    matches = [
        entry for entry in entries
        if isinstance(entry, dict) and str(entry.get("id") or "") == target_id
    ]
    return matches[0] if len(matches) == 1 else None


def default_group_policy_closed() -> bool:
    return all(
        feishu_transport.hermes_config_json(key) == "disabled"
        for key in ("feishu.default_group_policy", "platforms.feishu.extra.default_group_policy")
    )


def feishu_ssl_context() -> ssl.SSLContext:
    for candidate in (
        Path("/etc/ssl/cert.pem"),
        Path("/opt/homebrew/etc/ca-certificates/cert.pem"),
        Path("/opt/homebrew/etc/openssl@3/cert.pem"),
    ):
        if candidate.is_file():
            try:
                return ssl.create_default_context(cafile=str(candidate))
            except (OSError, ssl.SSLError):
                continue
    try:
        return ssl.create_default_context()
    except (OSError, ssl.SSLError) as exc:
        raise EmailSyncError("tls_trust_unavailable", "Feishu TLS trust store is unavailable") from exc


def live_feishu_chat_lookup(config: dict[str, Any], target_id: str) -> tuple[Optional[dict[str, Any]], str]:
    profile_root = runtime_paths(config)["profile_root"]
    env_path = profile_root / ".env"
    app_id = os.environ.get("FEISHU_APP_ID") or feishu_transport.load_env_value(env_path, "FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or feishu_transport.load_env_value(env_path, "FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        return None, "credentials_unavailable"
    context = feishu_ssl_context()
    token_request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(token_request, timeout=15, context=context) as response:
            token_payload = json.load(response)
        token = token_payload.get("tenant_access_token") if isinstance(token_payload, dict) else None
        if not token:
            return None, "token_unavailable"
        request = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/chats/"
            + urllib.parse.quote(target_id, safe=""),
            headers={"Authorization": "Bearer " + str(token)},
        )
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            payload = json.load(response)
    except urllib.error.HTTPError:
        return None, "chat_lookup_rejected"
    except (TimeoutError, urllib.error.URLError, OSError, ValueError):
        return None, "chat_lookup_failed"
    if not isinstance(payload, dict):
        return None, "chat_lookup_rejected"
    data = payload.get("data")
    if payload.get("code") != 0 or not isinstance(data, dict):
        return None, "chat_lookup_rejected"
    return {
        "name": data.get("name"),
        "chat_mode": data.get("chat_mode"),
        "chat_type": data.get("chat_type"),
    }, "ok"


def target_preflight(config: dict[str, Any], *, live_check: bool = True) -> dict[str, Any]:
    section = config["email_sync"]
    paths = runtime_paths(config)
    profile = profile_paths(config)
    mapping = read_json(paths["target_map"], {})
    if not isinstance(mapping, dict) or not file_mode_is_0600(paths["target_map"]):
        raise EmailSyncError("target_map_missing", "email report target map is missing or unsafe")
    target_id = str(mapping.get("channel_id") or "")
    expected_name = str(section["expected_group_name"])
    expected_alias = str(section["group_alias"])
    if not TARGET_ID_RE.fullmatch(target_id):
        raise EmailSyncError("target_map_unverified", "email report target identity is invalid")
    if (
        mapping.get("schema_version") != 1
        or mapping.get("platform") != "feishu"
        or mapping.get("alias") != expected_alias
        or mapping.get("verified_group_name") != expected_name
        or mapping.get("chat_mode") != "group"
        or mapping.get("chat_type") != "private"
        or mapping.get("allowlisted") is not True
        or mapping.get("api_verified") is not True
    ):
        raise EmailSyncError("target_map_unverified", "email report target map is not fully verified")
    validated = parse_datetime(mapping.get("validated_at"))
    if not validated or (
        not live_check
        and datetime.now().astimezone() - validated > TARGET_VALIDATION_MAX_AGE
    ):
        raise EmailSyncError("target_validation_stale", "email report target validation is stale")

    aliases = read_json(profile["aliases"], {})
    feishu_aliases = aliases.get("feishu") if isinstance(aliases, dict) else None
    if not isinstance(feishu_aliases, dict) or feishu_aliases.get(target_id) != expected_alias:
        raise EmailSyncError("target_alias_missing", "email report target alias is missing")
    if not file_mode_is_0600(profile["aliases"]):
        raise EmailSyncError("target_alias_permissions", "email report target alias permissions are unsafe")

    directory = read_json(profile["directory"], {})
    updated = parse_datetime(directory.get("updated_at")) if isinstance(directory, dict) else None
    if not updated or datetime.now().astimezone() - updated > DIRECTORY_MAX_AGE:
        raise EmailSyncError("channel_directory_stale", "Feishu channel directory is stale")
    entry = directory_entry(config, target_id)
    if (
        not entry
        or entry.get("name") != expected_alias
        or entry.get("type") not in {"group", "dm"}
    ):
        raise EmailSyncError("channel_directory_target_missing", "email report target is absent from the live directory")
    directory_is_placeholder = entry.get("type") != "group"
    if directory_is_placeholder and not live_check:
        raise EmailSyncError(
            "channel_directory_target_unverified",
            "Feishu alias placeholder requires a live private-group check",
        )
    if not feishu_transport.group_allowlist_matches(target_id):
        raise EmailSyncError("group_allowlist_missing", "email report target allowlist does not match")
    if not default_group_policy_closed():
        raise EmailSyncError("default_group_policy_not_closed", "Feishu default group policy is not closed")
    if not feishu_transport.gateway_running():
        raise EmailSyncError("gateway_not_running", "Feishu gateway is not running")
    if live_check:
        info, reason = live_feishu_chat_lookup(config, target_id)
        if not info:
            raise EmailSyncError("live_target_lookup_failed", f"live Feishu target check failed: {reason}")
        if (
            info.get("name") != expected_name
            or info.get("chat_mode") != "group"
            or info.get("chat_type") != "private"
        ):
            raise EmailSyncError("live_target_mismatch", "live Feishu target identity changed")
    return {
        "target_verified": True,
        "group_name": expected_name,
        "private_group": True,
        "directory_placeholder_verified_live": directory_is_placeholder,
    }


def bootstrap_target(config: dict[str, Any]) -> dict[str, Any]:
    section = config["email_sync"]
    paths = runtime_paths(config)
    profile = profile_paths(config)
    seed = read_json(paths["target_map"], {})
    if not isinstance(seed, dict) or not file_mode_is_0600(paths["target_map"]):
        raise EmailSyncError("target_map_missing", "email target seed map is missing or unsafe")
    target_id = str(seed.get("channel_id") or "")
    if not TARGET_ID_RE.fullmatch(target_id):
        raise EmailSyncError("target_id_invalid", "configured email report target is invalid")
    info, reason = live_feishu_chat_lookup(config, target_id)
    if not info:
        raise EmailSyncError("chat_lookup_failed", f"Feishu target lookup failed: {reason}")
    if (
        info.get("name") != section["expected_group_name"]
        or info.get("chat_mode") != "group"
        or info.get("chat_type") != "private"
    ):
        raise EmailSyncError("bootstrap_target_mismatch", "Feishu target is not the expected private group")

    backup_root = paths["state_root"] / "bootstrap-backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    ensure_private_dir(backup_root)
    tracked = [profile["config"], profile["aliases"], paths["target_map"]]
    journal: list[dict[str, Any]] = []
    for index, target in enumerate(tracked, 1):
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise EmailSyncError("unsafe_bootstrap_target", "Feishu configuration target is unsafe")
            backup = backup_root / f"{index:02d}-{safe_filename(target.name)}"
            shutil.copyfile(target, backup)
            backup.chmod(0o600)
            journal.append({"target": target, "backup": backup, "existed": True, "mode": stat.S_IMODE(target.stat().st_mode)})
        else:
            journal.append({"target": target, "backup": None, "existed": False, "mode": 0o600})
    try:
        allowed, allow_reason = feishu_transport.ensure_group_allowlist(target_id)
        if not allowed:
            raise EmailSyncError("group_allowlist_write_failed", f"Feishu allowlist update failed: {allow_reason}")
        aliases = read_json(profile["aliases"], {})
        if not isinstance(aliases, dict):
            aliases = {}
        feishu_aliases = aliases.setdefault("feishu", {})
        if not isinstance(feishu_aliases, dict):
            feishu_aliases = {}
            aliases["feishu"] = feishu_aliases
        feishu_aliases[target_id] = str(section["group_alias"])
        atomic_write_text(profile["aliases"], json.dumps(aliases, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        mapping = {
            "schema_version": 1,
            "platform": "feishu",
            "alias": section["group_alias"],
            "channel_id": target_id,
            "verified_group_name": section["expected_group_name"],
            "chat_mode": "group",
            "chat_type": "private",
            "allowlisted": True,
            "api_verified": True,
            "validated_at": now_iso(),
        }
        atomic_write_text(paths["target_map"], json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if not feishu_transport.group_allowlist_matches(target_id):
            raise EmailSyncError("group_allowlist_verify_failed", "Feishu allowlist did not persist")
        if not default_group_policy_closed():
            raise EmailSyncError("default_group_policy_not_closed", "Feishu default group policy changed")
    except Exception:
        for entry in reversed(journal):
            target = Path(entry["target"])
            if entry["existed"]:
                backup = Path(entry["backup"])
                atomic_write_text(target, backup.read_text(encoding="utf-8"), mode=int(entry["mode"]))
            elif target.exists() and target.is_file() and not target.is_symlink():
                target.unlink()
        raise
    entry = directory_entry(config, target_id)
    return {
        "target_verified": True,
        "allowlisted": True,
        "alias_registered": True,
        "directory_ready": bool(
            entry
            and entry.get("name") == section["group_alias"]
            and entry.get("type") in {"group", "dm"}
        ),
        "group_name": section["expected_group_name"],
    }


def current_validation_for_collection() -> dict[str, Any]:
    """Reuse a complete, current receipt, never a success flag or stale snapshot."""
    import validation_evidence
    from automatic_memory_validation import index_state
    root = BRIDGE_ROOT
    success = json.loads((root / "state/last-memory-validation-success.json").read_text())
    index = index_state(root)
    current = validation_evidence.receipt_status(root, success, index["dirty"], index["applied"])
    if not current["current"]:
        code = current.get("error_code")
        raise EmailSyncError(code if code in {"validation_permission_denied", "validation_context_unavailable"} else "memory_core_unverified", "current validation is required before mail discovery")
    accepted = datetime.fromisoformat(str(success["recorded_at"]).replace("Z", "+00:00"))
    if accepted.tzinfo is None:
        raise EmailSyncError("validation_receipt_unproven", "validation timestamp lacks timezone")
    for name, field in [("last-memory-validation-failure.json", "recorded_at"), ("validation-request.json", "requested_at")]:
        path = root / "state" / name
        if path.exists():
            record = json.loads(path.read_text())
            at = datetime.fromisoformat(str(record[field]).replace("Z", "+00:00"))
            if at.tzinfo is None or at >= accepted:
                raise EmailSyncError("memory_core_unverified", "newer validation work is unresolved")
    return {"validator": {"verdict": "pass"}, "current_full_receipt_reused": True}


def memory_health_preflight(config: dict[str, Any]) -> dict[str, Any]:
    if maintenance_active():
        raise EmailSyncError("maintenance_active", "business memory maintenance is active")
    try:
        lock = memory.acquire_lock(memory.MEMORY_LOCK, blocking=False)
    except memory.AutomationError as exc:
        raise EmailSyncError("memory_lock_busy", "business memory writer lock is busy") from exc
    try:
        if maintenance_active():
            raise EmailSyncError("maintenance_active", "business memory maintenance became active")
        import maintenance_mode as maintenance  # noqa: E402

        queues = maintenance.queue_counts()
        if any(int(value or 0) for value in queues.values()):
            raise EmailSyncError("memory_queues_pending", "business memory queues are not empty")
        database = maintenance.database_health(config)
        if heavy_work_ready(config):
            validation = memory.run_refresh_and_validate(
                config, lock_held=True, refresh_indexes=False, attempts=1
            )
            memory.memory_records(runtime_paths(config)["memory_root"], strict=True)
        else:
            validation = current_validation_for_collection()
        return {
            "memory_lock": "ok",
            "queues_empty": True,
            "database": database,
            "validator": str((validation.get("validator") or {}).get("verdict") or "unknown"),
        }
    except EmailSyncError:
        raise
    except (memory.AutomationError, OSError, sqlite3.Error, ValueError) as exc:
        raise EmailSyncError(getattr(exc, "code", "memory_health_failed"), "business memory health check failed") from exc
    finally:
        lock.close()


def run_preflight(
    config: dict[str, Any], *, require_enabled: bool = True,
    recover_interrupted: bool = False, include_reporting: bool = True
) -> dict[str, Any]:
    if maintenance_active():
        raise EmailSyncError("maintenance_active", "business memory maintenance is active")
    if require_enabled and config["email_sync"].get("enabled") is not True:
        raise EmailSyncError("email_sync_disabled", "enterprise email sync is disabled")
    if require_enabled and config["email_sync"].get("writer_registered") is not True:
        raise EmailSyncError("writer_not_registered", "email memory writer is not registered with maintenance")
    safety = read_safety_status(config)
    if not safety["server_side_pass"] or not safety["cross_device_confirmed"]:
        raise EmailSyncError("read_safety_unconfirmed", "cross-device unread-state safety is not confirmed")
    mailbox = mailbox_preflight(config)
    target = target_preflight(config, live_check=True) if include_reporting else {
        "target_verified": False,
        "local_only": True,
    }
    recovered_transactions = recover_memory_transactions(config) if recover_interrupted else 0
    memory_health = memory_health_preflight(config)
    return {
        "status": "pass",
        "mailbox": mailbox,
        "read_safety": safety,
        "memory": memory_health,
        "feishu": target,
        "recovered_transactions": recovered_transactions,
    }


def batch_stats(config: dict[str, Any], batch_id: str) -> dict[str, Any]:
    connection = connect_database(config)
    try:
        row = connection.execute(
            """
            SELECT b.*,
              COUNT(m.message_key) AS actual_count,
              SUM(CASE WHEN m.direction='incoming' THEN 1 ELSE 0 END) AS incoming_count,
              SUM(CASE WHEN m.direction='outgoing' THEN 1 ELSE 0 END) AS outgoing_count,
              SUM(CASE WHEN m.archive_state='archived' THEN 1 ELSE 0 END) AS archived_count_now,
              SUM(CASE WHEN m.memory_state='applied' THEN 1 ELSE 0 END) AS memory_count_now,
              SUM(CASE WHEN m.archive_state='failed' OR (m.archive_state='archived' AND m.memory_state!='applied' AND m.last_error_code IS NOT NULL) THEN 1 ELSE 0 END) AS failed_count_now,
              SUM(CASE WHEN m.archive_state='discovered' OR (m.archive_state='archived' AND m.memory_state!='applied' AND m.last_error_code IS NULL) THEN 1 ELSE 0 END) AS unattempted_count
            FROM batches b
            JOIN batch_messages bm ON bm.batch_id=b.batch_id
            JOIN messages m ON m.message_key=bm.message_key
            WHERE b.batch_id=? GROUP BY b.batch_id
            """,
            (batch_id,),
        ).fetchone()
        if not row:
            raise EmailSyncError("batch_missing", "email batch does not exist")
        attachments = connection.execute(
            """
            SELECT COUNT(*) AS total,
              SUM(CASE WHEN a.status IN ('verified','needs_review') AND length(a.sha256)=64 THEN 1 ELSE 0 END) AS verified,
              SUM(CASE WHEN a.status='pending' OR length(a.sha256)!=64 THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN a.status='needs_review' AND length(a.sha256)=64 THEN 1 ELSE 0 END) AS needs_review
            FROM attachments a JOIN batch_messages bm ON bm.message_key=a.message_key
            WHERE bm.batch_id=?
            """,
            (batch_id,),
        ).fetchone()
        semantics = connection.execute(
            """
            SELECT COALESCE(SUM(source_summary_written),0) AS source_notes,
              COALESCE(SUM(memory_writes),0) AS memory_writes,
              COALESCE(SUM(pending_candidates),0) AS pending_candidates,
              COALESCE(SUM(linked_existing),0) AS linked_existing,
              COALESCE(SUM(conflicts),0) AS conflicts
            FROM semantic_groups WHERE batch_id=? AND status='applied'
            """,
            (batch_id,),
        ).fetchone()
        phase_counts = email_phases.counts(connection, batch_id)
        project_rows = connection.execute(
            "SELECT project_updates_json FROM semantic_groups WHERE batch_id=? AND status='applied' ORDER BY created_at",
            (batch_id,),
        ).fetchall()
    finally:
        connection.close()
    projects: list[dict[str, str]] = []
    seen_projects: set[str] = set()
    for project_row in project_rows:
        values = read_json_text(str(project_row["project_updates_json"] or "[]"), [])
        for value in values if isinstance(values, list) else []:
            if not isinstance(value, dict):
                continue
            name = public_report_text(value.get("name"), 80)
            progress = public_report_text(value.get("progress"), 220)
            status_name = str(value.get("status") or "pending")
            if status_name not in {"source_observed", "pending", "conflict"}:
                status_name = "pending"
            key = sha256_text(f"{name}|{progress}|{status_name}")
            if name and progress and key not in seen_projects:
                seen_projects.add(key)
                projects.append(
                    {"name": name, "progress": progress, "status": status_name}
                )
    return {
        "batch_id": batch_id,
        "mode": str(row["mode"]),
        "report_policy": str(row["report_policy"] or "normal"),
        "status": str(row["status"]),
        "discovered": int(row["actual_count"] or 0),
        "incoming": int(row["incoming_count"] or 0),
        "outgoing": int(row["outgoing_count"] or 0),
        "archived": int(row["archived_count_now"] or 0),
        "memory_applied": int(row["memory_count_now"] or 0),
        "failed": int(row["failed_count_now"] or 0),
        "unattempted": int(row["unattempted_count"] or 0),
        "attachments": int(attachments["total"] or 0),
        "attachments_verified": int(attachments["verified"] or 0),
        "attachments_pending": int(attachments["pending"] or 0),
        "attachments_needs_review": int(attachments["needs_review"] or 0),
        "source_notes": int(semantics["source_notes"] or 0),
        "memory_writes": int(semantics["memory_writes"] or 0),
        "pending_candidates": int(semantics["pending_candidates"] or 0),
        "linked_existing": int(semantics["linked_existing"] or 0),
        "conflicts": int(semantics["conflicts"] or 0),
        "projects": projects[:5],
        "phases": phase_counts,
        "processing_complete": int(row["memory_count_now"] or 0) == int(row["actual_count"] or 0) and not phase_counts.get("pending"),
    }


def read_json_text(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def refresh_batch_counts(config: dict[str, Any]) -> int:
    connection = connect_database(config)
    try:
        batch_ids = [str(row[0]) for row in connection.execute(
            "SELECT batch_id FROM batches WHERE status NOT IN ('reported','local_only_complete') ORDER BY created_at"
        )]
    finally:
        connection.close()
    updated = 0
    for batch_id in batch_ids:
        stats = batch_stats(config, batch_id)
        local_only = stats["report_policy"] == "local_only"
        if local_only and stats.get("processing_complete", stats["memory_applied"] == stats["discovered"]):
            state = "local_only_complete"
        elif local_only and stats["unattempted"] == 0:
            state = "local_only_partial"
        elif stats.get("processing_complete", stats["memory_applied"] == stats["discovered"]):
            state = "ready_report"
        elif stats["unattempted"] == 0:
            state = "partial_ready"
        else:
            state = "processing"
        connection = connect_database(config)
        try:
            current = connection.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            preserved = str(current["status"]) if current and str(current["status"]) in {
                "partial_reported", "report_failed", "report_uncertain"
            } else state
            connection.execute(
                """
                UPDATE batches SET discovered_count=?,archived_count=?,memory_applied_count=?,
                  failed_count=?,status=? WHERE batch_id=?
                """,
                (
                    stats["discovered"], stats["archived"], stats["memory_applied"],
                    stats["failed"], preserved, batch_id,
                ),
            )
            if local_only and stats.get("processing_complete", stats["memory_applied"] == stats["discovered"]):
                stamp = now_iso()
                connection.execute(
                    """
                    UPDATE messages SET report_state='local_only',reported_at=COALESCE(reported_at,?),updated_at=?
                    WHERE message_key IN (SELECT message_key FROM batch_messages WHERE batch_id=?)
                      AND report_state='pending'
                    """,
                    (stamp, stamp, batch_id),
                )
                if stats["mode"] == "baseline":
                    set_meta(connection, "baseline_complete", "true")
                    set_meta(connection, "baseline_completed_at", stamp)
            connection.commit()
            updated += 1
        finally:
            connection.close()
    return updated


def compose_report(stats: dict[str, Any], *, correction: bool = False) -> str:
    kind = "基线更正" if correction else ("首次基线" if stats["mode"] == "baseline" else "增量批次")
    pending_messages = max(0, stats["discovered"] - stats["memory_applied"])
    section_one = (
        f"- {kind}发现企业邮件 {stats['discovered']} 封（新收件 {stats['incoming']} 封、同会话已发送 {stats['outgoing']} 封）；"
        f"已归档正文 {stats['archived']} 封、当前可用内容入库 {stats['memory_applied']} 封"
        + (f"，待重试 {pending_messages} 封。" if pending_messages else "。")
    )
    section_two = (
        f"- 更新邮件来源归档 {stats['archived']} 份；附件 {stats['attachments_verified']}/{stats['attachments']} 份已校验；"
        f"业务记忆来源索引 {stats['source_notes']} 条、待确认 {stats['pending_candidates']} 条、关联既有记忆 {stats['linked_existing']} 条。"
    )
    if stats["attachments_pending"]:
        section_two += f" 另有附件 {stats['attachments_pending']} 份待重试。"
    if stats.get("attachments_needs_review"):
        section_two += f" 风险附件 {stats['attachments_needs_review']} 份已保留原件并标记待处理。"
    if stats.get("phases", {}).get("total") or stats.get("phases", {}).get("pending"):
        phase = stats["phases"]
        section_two += (f" 图片待处理 {phase['image_pending']} 项，附件等待 Win11 {phase['win11_pending']} 项，"
                        f"需人工处理 {phase['needs_review']} 项，外链或内嵌图片缺口 {phase.get('reference_gaps',0)} 项，历史来源缺口 {phase.get('source_gaps',0)} 封，待补录 {phase['awaiting_supplement']} 项；补录完成 {phase['completed']} 项。")
    status_labels = {
        "source_observed": "邮件来源显示，尚未确认",
        "pending": "待确认",
        "conflict": "信息存在冲突，待核实",
    }
    project_lines = [
        f"- {item['name']}（{status_labels.get(str(item.get('status')), '待确认')}）：{item['progress']}"
        for item in stats["projects"]
    ]
    if not project_lines:
        project_lines = ["- 暂无可确认项目进展；邮件陈述未自动升级为公司确认事实。"]
    report = (
        "[企业邮件同步 | MISO]\n"
        "① 做了哪些事情\n" + section_one + "\n\n"
        "② 修改了哪些文件\n" + section_two + "\n\n"
        "③ 哪些项目有进展\n" + "\n".join(project_lines) + "\n\n"
        "—— MISO"
    )
    if (
        any(report.count(heading) != 1 for heading in (
            "① 做了哪些事情", "② 修改了哪些文件", "③ 哪些项目有进展"
        ))
        or
        EMAIL_RE.search(report)
        or MOBILE_RE.search(report)
        or TARGET_ID_RE.search(report)
        or PUBLIC_INTERNAL_ID_RE.search(report)
        or LOCAL_PATH_RE.search(report)
        or "email-conversation:" in report
    ):
        raise EmailSyncError("report_privacy", "email report privacy validation failed")
    return report


def queue_report(
    config: dict[str, Any], batch_id: str, revision: int, event_kind: str,
    report: str, *, is_complete: bool
) -> bool:
    connection = connect_database(config)
    try:
        if connection.execute("SELECT 1 FROM outbox WHERE batch_id=? AND revision=?", (batch_id, revision)).fetchone():
            return False
        stamp = now_iso()
        event_hash = sha256_text(f"{batch_id}|{revision}|{report}")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO outbox(event_id,batch_id,revision,event_kind,is_complete,report_text,event_hash,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (str(uuid.uuid4()), batch_id, revision, event_kind, int(is_complete), report, event_hash, stamp, stamp),
        )
        connection.execute(
            "UPDATE batches SET report_revision=?,status='awaiting_report' WHERE batch_id=?",
            (revision, batch_id),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reconcile_reports(config: dict[str, Any]) -> int:
    connection = connect_database(config)
    try:
        batches = connection.execute(
            "SELECT * FROM batches WHERE report_policy='normal' ORDER BY created_at"
        ).fetchall()
    finally:
        connection.close()
    queued = 0
    for batch in batches:
        batch_id = str(batch["batch_id"])
        stats = batch_stats(config, batch_id)
        ready = stats["unattempted"] == 0
        if not ready:
            continue
        connection = connect_database(config)
        try:
            events = connection.execute(
                "SELECT revision,status FROM outbox WHERE batch_id=? ORDER BY revision", (batch_id,)
            ).fetchall()
        finally:
            connection.close()
        if not events:
            kind = "baseline" if stats["mode"] == "baseline" else "incremental"
            queued += int(queue_report(
                config, batch_id, 0, kind, compose_report(stats),
                is_complete=stats.get("processing_complete", stats["memory_applied"] == stats["discovered"]),
            ))
            continue
        initial = next((event for event in events if int(event["revision"]) == 0), None)
        correction_exists = any(int(event["revision"]) == 1 for event in events)
        complete = stats.get("processing_complete", stats["memory_applied"] == stats["discovered"])
        if initial and initial["status"] == "delivered" and complete and str(batch["status"]) == "partial_reported" and not correction_exists:
            queued += int(queue_report(
                config, batch_id, 1, "correction", compose_report(stats, correction=True), is_complete=True
            ))
    return queued


def send_feishu_direct(config: dict[str, Any], report: str) -> dict[str, str]:
    mapping = read_json(runtime_paths(config)["target_map"], {})
    target_id = str(mapping.get("channel_id") or "") if isinstance(mapping, dict) else ""
    profile_root = runtime_paths(config)["profile_root"]
    env_path = profile_root / ".env"
    app_id = os.environ.get("FEISHU_APP_ID") or feishu_transport.load_env_value(env_path, "FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or feishu_transport.load_env_value(env_path, "FEISHU_APP_SECRET")
    if not app_id or not app_secret or not TARGET_ID_RE.fullmatch(target_id):
        return {"status": "failed", "reason_code": "credentials_unavailable"}
    token_request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    context = feishu_ssl_context()
    try:
        with urllib.request.urlopen(token_request, timeout=15, context=context) as response:
            token_payload = json.load(response)
    except (TimeoutError, urllib.error.URLError, OSError, ValueError):
        return {"status": "failed", "reason_code": "token_request_failed"}
    token = token_payload.get("tenant_access_token") if isinstance(token_payload, dict) else None
    if not token:
        return {"status": "failed", "reason_code": "token_unavailable"}
    request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=json.dumps(
            {"receive_id": target_id, "msg_type": "text", "content": json.dumps({"text": report}, ensure_ascii=False)},
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Authorization": "Bearer " + str(token), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            payload = json.load(response)
    except urllib.error.HTTPError:
        return {"status": "failed", "reason_code": "feishu_rejected"}
    except (TimeoutError, urllib.error.URLError, OSError, ValueError):
        return {"status": "uncertain", "reason_code": "send_timeout_or_network"}
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return {"status": "failed", "reason_code": "feishu_rejected"}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    message_id = str(data.get("message_id") or "")
    if not message_id:
        return {"status": "uncertain", "reason_code": "message_id_missing"}
    return {"status": "delivered", "receipt_hash": sha256_text(message_id)[:16]}


def next_outbox_event(config: dict[str, Any]) -> Optional[sqlite3.Row]:
    connection = connect_database(config)
    try:
        return connection.execute(
            """
            SELECT * FROM outbox
            WHERE status='pending'
               OR (status='failed' AND attempt_count<? AND next_attempt_at IS NOT NULL AND next_attempt_at<=?)
            ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,created_at,event_id LIMIT 1
            """,
            (MAX_DELIVERY_ATTEMPTS, now_iso()),
        ).fetchone()
    finally:
        connection.close()


def claim_next_outbox_event(config: dict[str, Any]) -> Optional[sqlite3.Row]:
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        event = connection.execute(
            """
            SELECT * FROM outbox
            WHERE status='pending'
               OR (status='failed' AND attempt_count<? AND next_attempt_at IS NOT NULL AND next_attempt_at<=?)
            ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,created_at,event_id LIMIT 1
            """,
            (MAX_DELIVERY_ATTEMPTS, now_iso()),
        ).fetchone()
        if not event:
            connection.commit()
            return None
        connection.execute(
            "UPDATE outbox SET status='sending',next_attempt_at=NULL,updated_at=? WHERE event_id=?",
            (now_iso(), str(event["event_id"])),
        )
        claimed = connection.execute(
            "SELECT * FROM outbox WHERE event_id=?", (str(event["event_id"]),)
        ).fetchone()
        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def recover_stuck_deliveries(config: dict[str, Any]) -> int:
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT event_id,batch_id FROM outbox WHERE status='sending' ORDER BY created_at,event_id"
        ).fetchall()
        if not rows:
            connection.commit()
            return 0
        stamp = now_iso()
        batch_ids = sorted({str(row["batch_id"]) for row in rows})
        connection.execute(
            """
            UPDATE outbox SET status='uncertain',attempt_count=attempt_count+1,
              last_error_code='delivery_outcome_unknown_after_interruption',next_attempt_at=NULL,updated_at=?
            WHERE status='sending'
            """,
            (stamp,),
        )
        for batch_id in batch_ids:
            connection.execute(
                """
                UPDATE batches SET status='report_uncertain',
                  last_error_code='delivery_outcome_unknown_after_interruption'
                WHERE batch_id=?
                """,
                (batch_id,),
            )
            connection.execute(
                """
                UPDATE messages SET report_state='uncertain',updated_at=?
                WHERE message_key IN (SELECT message_key FROM batch_messages WHERE batch_id=?)
                  AND report_state='pending'
                """,
                (stamp, batch_id),
            )
        connection.commit()
        return len(rows)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_delivery(config: dict[str, Any], event: sqlite3.Row, result: dict[str, str]) -> None:
    status_name = str(result.get("status") or "uncertain")
    reason = str(result.get("reason_code") or "")[:80] or None
    stamp = now_iso()
    receipt_hash = str(result.get("receipt_hash") or "")
    delivery_stats = (
        batch_stats(config, str(event["batch_id"]))
        if status_name == "delivered" and re.fullmatch(r"[0-9a-f]{16}", receipt_hash)
        else None
    )
    connection = connect_database(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if status_name == "delivered":
            if not re.fullmatch(r"[0-9a-f]{16}", receipt_hash):
                status_name = "uncertain"
                reason = "receipt_invalid"
            else:
                connection.execute(
                    """
                    UPDATE outbox SET status='delivered',attempt_count=attempt_count+1,receipt_hash=?,
                      last_error_code=NULL,updated_at=?,delivered_at=? WHERE event_id=?
                    """,
                    (receipt_hash, stamp, stamp, str(event["event_id"])),
                )
                stats = delivery_stats or {}
                complete = bool(event["is_complete"]) and stats.get("processing_complete", stats["memory_applied"] == stats["discovered"])
                batch_status = "reported" if complete else "partial_reported"
                connection.execute(
                    "UPDATE batches SET status=?,completed_at=? WHERE batch_id=?",
                    (batch_status, stamp if complete else None, str(event["batch_id"])),
                )
                if complete:
                    connection.execute(
                        """
                        UPDATE messages SET report_state='reported',reported_at=?,updated_at=?
                        WHERE message_key IN (SELECT message_key FROM batch_messages WHERE batch_id=?)
                          AND memory_state='applied'
                        """,
                        (stamp, stamp, str(event["batch_id"])),
                    )
                if complete and stats["mode"] == "baseline":
                    set_meta(connection, "baseline_complete", "true")
                    set_meta(connection, "baseline_completed_at", stamp)
                connection.commit()
                return
        if status_name == "uncertain":
            connection.execute(
                """
                UPDATE outbox SET status='uncertain',attempt_count=attempt_count+1,last_error_code=?,
                  next_attempt_at=NULL,updated_at=? WHERE event_id=?
                """,
                (reason or "delivery_uncertain", stamp, str(event["event_id"])),
            )
            connection.execute(
                "UPDATE batches SET status='report_uncertain',last_error_code=? WHERE batch_id=?",
                (reason or "delivery_uncertain", str(event["batch_id"])),
            )
            connection.execute(
                """
                UPDATE messages SET report_state='uncertain',updated_at=?
                WHERE message_key IN (SELECT message_key FROM batch_messages WHERE batch_id=?)
                  AND report_state='pending'
                """,
                (stamp, str(event["batch_id"])),
            )
        else:
            retryable = (reason or "") in RETRYABLE_DELIVERY_ERRORS
            attempts = int(event["attempt_count"] or 0) + 1
            next_attempt = (
                (datetime.now().astimezone() + DELIVERY_RETRY_DELAY).isoformat(timespec="seconds")
                if retryable and attempts < MAX_DELIVERY_ATTEMPTS else None
            )
            connection.execute(
                """
                UPDATE outbox SET status='failed',attempt_count=?,last_error_code=?,next_attempt_at=?,updated_at=?
                WHERE event_id=?
                """,
                (attempts, reason or "delivery_failed", next_attempt, stamp, str(event["event_id"])),
            )
            connection.execute(
                "UPDATE batches SET status='report_failed',last_error_code=? WHERE batch_id=?",
                (reason or "delivery_failed", str(event["batch_id"])),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def deliver_outbox(config: dict[str, Any]) -> dict[str, int]:
    delivered = failed = uncertain = 0
    for _ in range(MAX_OUTBOX_EVENTS_PER_RUN):
        event = claim_next_outbox_event(config)
        if not event:
            break
        try:
            target_preflight(config, live_check=True)
            result = send_feishu_direct(config, str(event["report_text"]))
        except EmailSyncError as exc:
            result = {"status": "failed", "reason_code": exc.code}
        if result.get("status") == "delivered" and not re.fullmatch(
            r"[0-9a-f]{16}", str(result.get("receipt_hash") or "")
        ):
            result = {"status": "uncertain", "reason_code": "receipt_invalid"}
        record_delivery(config, event, result)
        delivered += int(result.get("status") == "delivered")
        failed += int(result.get("status") == "failed")
        uncertain += int(result.get("status") == "uncertain")
        if result.get("status") != "delivered":
            break
    return {"delivered": delivered, "failed": failed, "uncertain": uncertain}


def outstanding_batch_ids(config: dict[str, Any]) -> list[str]:
    connection = connect_database(config)
    try:
        return [str(row[0]) for row in connection.execute(
            """
            SELECT DISTINCT b.batch_id FROM batches b
            JOIN batch_messages bm ON bm.batch_id=b.batch_id
            JOIN messages m ON m.message_key=bm.message_key
            WHERE m.archive_state!='archived' OR m.memory_state!='applied'
            ORDER BY b.created_at
            """
        )]
    finally:
        connection.close()


def status_payload(config: dict[str, Any]) -> dict[str, Any]:
    database_path = runtime_paths(config)["database"]
    if not database_path.is_file() or database_path.is_symlink():
        return {
            "status": "not_initialized",
            "maintenance_active": maintenance_active(),
            "enabled": config["email_sync"].get("enabled") is True,
            "writer_registered": config["email_sync"].get("writer_registered") is True,
            "read_safety": read_safety_status(config),
            "baseline_complete": False,
            "message_total": 0,
            "archive_states": {},
            "archive_retry_states": {},
            "memory_states": {},
            "report_states": {},
            "attachment_states": {},
            "batch_states": {},
            "outbox_states": {},
        }
    connection = connect_database_readonly(config)
    try:
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        messages = {
            str(row["state"]): int(row["count"])
            for row in connection.execute("SELECT archive_state AS state,COUNT(*) AS count FROM messages GROUP BY archive_state")
        }
        retry_states = {
            str(row["state"]): int(row["count"])
            for row in connection.execute(
                """
                SELECT CASE
                  WHEN archive_failure_class='transient' AND archive_retry_exhausted_at IS NOT NULL THEN 'low_frequency_probe'
                  WHEN archive_failure_class='transient' AND archive_next_retry_at IS NOT NULL THEN 'scheduled'
                  WHEN archive_retry_exhausted_at IS NOT NULL THEN 'frozen'
                  ELSE 'unclassified'
                END AS state,COUNT(*) AS count
                FROM messages WHERE archive_state='failed' GROUP BY state
                """
            )
        }
        memory_states = {
            str(row["state"]): int(row["count"])
            for row in connection.execute("SELECT memory_state AS state,COUNT(*) AS count FROM messages GROUP BY memory_state")
        }
        report_states = {
            str(row["state"]): int(row["count"])
            for row in connection.execute("SELECT report_state AS state,COUNT(*) AS count FROM messages GROUP BY report_state")
        }
        attachments = {
            str(row["state"]): int(row["count"])
            for row in connection.execute("SELECT status AS state,COUNT(*) AS count FROM attachments GROUP BY status")
        }
        outbox = {
            str(row["state"]): int(row["count"])
            for row in connection.execute("SELECT status AS state,COUNT(*) AS count FROM outbox GROUP BY status")
        }
        batches = {
            str(row["state"]): int(row["count"])
            for row in connection.execute("SELECT status AS state,COUNT(*) AS count FROM batches GROUP BY status")
        }
        phase_counts = email_phases.counts(connection)
        total = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        baseline_complete = get_meta(connection, "baseline_complete") == "true"
    finally:
        connection.close()
    return {
        "status": "ok" if quick == "ok" else "failed",
        "database_quick_check": quick,
        "database_user_version": user_version,
        "database_file_mode": oct(stat.S_IMODE(database_path.stat().st_mode)),
        "maintenance_active": maintenance_active(),
        "enabled": config["email_sync"].get("enabled") is True,
        "writer_registered": config["email_sync"].get("writer_registered") is True,
        "read_safety": read_safety_status(config),
        "baseline_complete": baseline_complete,
        "message_total": total,
        "archive_states": messages,
        "archive_retry_states": retry_states,
        "memory_states": memory_states,
        "report_states": report_states,
        "attachment_states": attachments,
        "phases": phase_counts,
        "recovery": mail_auto_recovery.summary(config),
        "batch_states": batches,
        "outbox_states": outbox,
    }


# 2020-01-02 Example Owner: plain mail and ordinary text attachments are processed
# at once; only big mail waits, and what it waits for is the Win11 reader being
# on power, not the Mac being idle. Size is the whole message as the mail list
# reports it, the only size known before archiving.
LIGHT_MESSAGE_MAX_BYTES = 10 * 1024 * 1024


def message_is_light(config: dict[str, Any], row: sqlite3.Row) -> bool:
    limit = int(config["email_sync"].get("light_message_max_bytes") or LIGHT_MESSAGE_MAX_BYTES)
    return int(row["size_bytes"] or 0) <= limit


def big_mail_ready(config: dict[str, Any]) -> bool:
    if win11_attachments.policy(config):
        return win11_attachments.example_bd50c44d55d3(config)
    return heavy_work_ready(config)  # no Win11 reader configured: the old rule


def heavy_work_ready(config: dict[str, Any]) -> bool:
    if config["email_sync"].get("background_collection_while_active") is not True:
        return True
    # Same idle detector as the scheduler; an unknown probe defers heavy work.
    from business_sync_supervisor import idle_state
    return idle_state().get("ready") is True


def pending_heavy_count(config: dict[str, Any]) -> int:
    connection = connect_database_readonly(config)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM messages WHERE archive_state IN ('discovered','failed') OR (archive_state='archived' AND memory_state='pending')").fetchone()[0])
    finally:
        connection.close()


def sync_once(config: dict[str, Any], *, report_policy: str = "normal") -> dict[str, Any]:
    if report_policy not in {"normal", "local_only"}:
        raise EmailSyncError("report_policy_invalid", "email report policy is invalid")
    local_only = report_policy == "local_only"
    lock = acquire_run_lock(blocking=False)
    try:
        preflight = run_preflight(
            config,
            require_enabled=True,
            recover_interrupted=heavy_work_ready(config),
            include_reporting=not local_only,
        )
        recovered_transactions = int(preflight.get("recovered_transactions") or 0)
        initialize(config)
        mail_auto_recovery.observe_generation(config)
        recovered_deliveries = 0 if local_only else recover_stuck_deliveries(config)
        mode, candidates = discover_candidates(config)
        batch_id, discovered_count = register_discovery(
            config, mode, candidates, report_policy=report_policy
        )
        archive_result = archive_pending(config)
        phase_backfill = 0
        phase_changes = email_phases.reconcile(config)
        automatic_repair = mail_semantic_recovery.reconcile(config) if mail_auto_recovery.enabled(config) else {}
        semantic_groups_superseded = int(automatic_repair.get('superseded') or 0)
        semantic_groups = int(automatic_repair.get('created') or 0)
        if not mail_auto_recovery.enabled(config):
            semantic_groups_superseded += supersede_expanded_legacy_semantic_groups(config)
            phase_batches = email_phases.pending_batches(config)
            for current_batch in dict.fromkeys(outstanding_batch_ids(config) + phase_batches):
                semantic_groups += ensure_semantic_groups(config, current_batch)
        if automatic_repair.get('block_all'):
            semantic_result = {'attempted': 0, 'failed': 0, 'model_calls': 0, 'memory_writes': 0}
        else:
            semantic_result = process_pending_semantics(config, exclude_ids=automatic_repair.get('blocked_semantic_ids') or [])
        mail_auto_recovery.record_semantic_blocks(config, automatic_repair)
        semantic_result['failed'] = int(semantic_result.get('failed') or 0) + int(automatic_repair.get('blocked') or 0)
        mail_auto_recovery.reconcile_ledger(config)
        # Background backfill registration follows foreground mail semantics.
        phase_backfill = email_phases.backfill(config)
        phase_workers = email_phases.start_workers(config)
        refresh_batch_counts(config)
        reports_queued = 0 if local_only else reconcile_reports(config)
        delivery = {"delivered": 0, "failed": 0, "uncertain": 0} if local_only else deliver_outbox(config)
        refresh_batch_counts(config)
        reports_queued += 0 if local_only else reconcile_reports(config)
        if not local_only and delivery["delivered"] and reports_queued:
            delivery_after_correction = deliver_outbox(config)
            delivery = {key: delivery[key] + delivery_after_correction[key] for key in delivery}
        did_work = any(
            [
                bool(batch_id), archive_result["attempted"] > 0, semantic_groups > 0,
                semantic_groups_superseded > 0,
                semantic_result["attempted"] > 0, reports_queued > 0,
                delivery["delivered"] > 0, delivery["failed"] > 0, delivery["uncertain"] > 0,
                recovered_transactions > 0, recovered_deliveries > 0, phase_workers > 0, phase_changes > 0,
            ]
        )
        phase_counts = email_phases.status_counts(config)
        return {
            "recovery": mail_auto_recovery.summary(config),
            "automatic_repair": automatic_repair,
            "phases": phase_counts,
            "status": "completed" if did_work else "noop",
            "mode": mode,
            "report_policy": report_policy,
            "local_only": local_only,
            "new_batch": bool(batch_id),
            "discovered": discovered_count,
            "archive": archive_result,
            "collection_only": False,
            # Only big mail can still be waiting, and only for the Win11 reader.
            "heavy_deferred": bool(archive_result.get("idle_deferred")),
            "deferred_reason": "waiting_win11_power" if archive_result.get("idle_deferred") else None,
            "phase_workers_started": phase_workers,
            "phase_supplements_changed": phase_changes,
            "phase_backfill_registered": phase_backfill,
            "semantic_groups_created": semantic_groups,
            "semantic_groups_superseded": semantic_groups_superseded,
            "semantic": semantic_result,
            "reports_queued": reports_queued,
            "delivery": delivery,
            "recovered_transactions": recovered_transactions,
            "recovered_delivery_uncertainties": recovered_deliveries,
            "preflight": {"status": preflight["status"]},
        }
    finally:
        lock.close()


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Governed enterprise email synchronization")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    commands.add_parser("sync-once")
    commands.add_parser("sync-local")
    commands.add_parser("status")
    commands.add_parser("bootstrap-target")
    confirm = commands.add_parser("confirm-read-safety")
    confirmation = confirm.add_mutually_exclusive_group(required=True)
    confirmation.add_argument("--still-unread", action="store_true")
    confirmation.add_argument("--changed-read", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "status":
            emit(status_payload(config))
        elif args.command == "preflight":
            emit(run_preflight(config, require_enabled=True))
        elif args.command == "bootstrap-target":
            emit({"status": "completed", **bootstrap_target(config)})
        elif args.command == "confirm-read-safety":
            emit({"status": "completed", **confirm_read_safety(config, bool(args.still_unread))})
        elif args.command == "sync-once":
            emit(sync_once(config, report_policy="normal"))
        elif args.command == "sync-local":
            emit(sync_once(config, report_policy="local_only"))
        return 0
    except EmailSyncError as exc:
        safe_status = "noop" if exc.code in {
            "already_running", "maintenance_active", "memory_lock_busy", "memory_queues_pending",
            "email_sync_disabled", "writer_not_registered", "read_safety_unconfirmed",
        } else "blocked"
        emit({"status": safe_status, "reason_code": exc.code})
        return 0 if args.command in {"sync-once", "sync-local"} else 2
    except (memory.AutomationError, OSError, sqlite3.Error, ValueError) as exc:
        emit({"status": "failed", "reason_code": getattr(exc, "code", type(exc).__name__)})
        return 2
    except Exception as exc:  # fail closed without exposing mail content or local identifiers
        code = re.sub(r"[^A-Za-z0-9_.:-]", "_", type(exc).__name__)[:80] or "unexpected_error"
        try:
            atomic_write_text(
                BRIDGE_ROOT / "state" / "email-sync-last-runtime-error.json",
                json.dumps(
                    {"schema_version": 1, "recorded_at": now_iso(), "command": args.command, "reason_code": code},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ) + "\n",
            )
        except OSError:
            pass
        emit({"status": "failed", "reason_code": code})
        return 2


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(cli())
