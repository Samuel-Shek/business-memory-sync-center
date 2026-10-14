#!/usr/bin/env python3
"""Read scoped WeChat 4.x history into a private local source database.

The production path is fail-closed: SIP must be enabled, WeChat must match the
one-time enrollment receipt, and every live encrypted database must have a
Keychain-backed key.  Tests use plaintext synthetic fixtures and never attach
to WeChat.
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
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from wechat_common import (
    RUN_STATES,
    WeChatSyncError,
    append_metadata_log,
    atomic_write,
    atomic_write_bytes,
    canonical_json,
    classify_message,
    ensure_private_dir,
    estimate_tokens,
    extract_pb_field_30,
    group_matches,
    now_iso,
    redact_text,
    render_message_content,
    sha256_bytes,
    stable_hash,
)


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
LOCK_PATH = ROOT / "state" / "wechat_chat_collect.lock"
MESSAGE_DB_RE = re.compile(r"^message_\d+\.db$")
MEDIA_DB_RE = re.compile(r"^media_\d+\.db$")
SENSITIVE_PROMPT_RE = re.compile(
    r"(?i)(密码|口令|验证码|校验码|token|secret|api[_-]?key|身份证号|银行卡号|"
    r"手机号).{0,16}(?:是多少|是什么|发我|给我|提供|[?？])"
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect scoped WeChat history read-only")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--mode", choices=("pilot", "full"))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--keep-working", action="store_true")
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeChatSyncError("invalid_config", "cannot read WeChat configuration") from exc
    chat = config.get("wechat_chat")
    if not isinstance(chat, dict):
        raise WeChatSyncError("invalid_config", "wechat_chat config is missing")
    return config


def acquire_lock() -> Any:
    ensure_private_dir(LOCK_PATH.parent)
    handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise WeChatSyncError("already_running", "another WeChat collection is already running") from exc
    return handle


def connect_store(path: Path) -> sqlite3.Connection:
    ensure_private_dir(path.parent)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA secure_delete=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sync_runs (
          run_id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL CHECK(status IN (
            'enrollment_required','collecting','media_pending','ready_for_ai',
            'paused_budget','failed','success'
          )),
          mode TEXT NOT NULL CHECK(mode IN ('pilot','full','preflight')),
          account_fingerprint TEXT NOT NULL,
          cutoff_epoch INTEGER NOT NULL,
          direct_count INTEGER NOT NULL DEFAULT 0,
          group_count INTEGER NOT NULL DEFAULT 0,
          conversations_complete INTEGER NOT NULL DEFAULT 0,
          conversations_incomplete INTEGER NOT NULL DEFAULT 0,
          messages_seen INTEGER NOT NULL DEFAULT 0,
          messages_inserted INTEGER NOT NULL DEFAULT 0,
          messages_updated INTEGER NOT NULL DEFAULT 0,
          redaction_count INTEGER NOT NULL DEFAULT 0,
          media_queued INTEGER NOT NULL DEFAULT 0,
          estimated_tokens INTEGER NOT NULL DEFAULT 0,
          error_code TEXT NOT NULL DEFAULT '',
          detail TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS scope_members (
          conversation_id TEXT PRIMARY KEY,
          account_fingerprint TEXT NOT NULL,
          source_username_sha256 TEXT NOT NULL,
          conversation_kind TEXT NOT NULL CHECK(conversation_kind IN ('direct','group')),
          display_name TEXT NOT NULL,
          match_reason TEXT NOT NULL,
          active INTEGER NOT NULL CHECK(active IN (0,1)),
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
          conversation_id TEXT PRIMARY KEY REFERENCES scope_members(conversation_id),
          account_fingerprint TEXT NOT NULL,
          conversation_kind TEXT NOT NULL CHECK(conversation_kind IN ('direct','group')),
          display_name TEXT NOT NULL,
          first_message_at INTEGER,
          last_message_at INTEGER,
          latest_scan_id TEXT,
          scan_complete INTEGER NOT NULL DEFAULT 0 CHECK(scan_complete IN (0,1)),
          current_visible INTEGER NOT NULL DEFAULT 1 CHECK(current_visible IN (0,1))
        );

        CREATE TABLE IF NOT EXISTS messages (
          message_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
          create_time INTEGER NOT NULL,
          source_shard TEXT NOT NULL,
          source_local_id TEXT NOT NULL,
          source_server_id_sha256 TEXT NOT NULL DEFAULT '',
          sender_name TEXT NOT NULL,
          sender_id_sha256 TEXT NOT NULL,
          message_type TEXT NOT NULL,
          text_redacted TEXT NOT NULL,
          structured_json TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          original_text_sha256 TEXT NOT NULL DEFAULT '',
          redaction_count INTEGER NOT NULL DEFAULT 0,
          current_visible INTEGER NOT NULL DEFAULT 1 CHECK(current_visible IN (0,1)),
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS messages_conversation_time
          ON messages(conversation_id, create_time);

        CREATE TABLE IF NOT EXISTS message_versions (
          message_id TEXT NOT NULL REFERENCES messages(message_id),
          content_sha256 TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          text_redacted TEXT NOT NULL,
          structured_json TEXT NOT NULL,
          original_text_sha256 TEXT NOT NULL DEFAULT '',
          redaction_count INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(message_id, content_sha256)
        );

        CREATE TABLE IF NOT EXISTS media_assets (
          asset_id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL REFERENCES messages(message_id),
          media_type TEXT NOT NULL,
          locator_json TEXT NOT NULL,
          locator_sha256 TEXT NOT NULL,
          local_path TEXT NOT NULL DEFAULT '',
          content_sha256 TEXT NOT NULL DEFAULT '',
          byte_size INTEGER NOT NULL DEFAULT 0,
          duration_seconds REAL,
          status TEXT NOT NULL CHECK(status IN ('queued','resolved','processed','unavailable','quarantined','failed')),
          error_code TEXT NOT NULL DEFAULT '',
          first_seen_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS media_derivations (
          derivation_id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL REFERENCES media_assets(asset_id),
          derivation_type TEXT NOT NULL,
          tool_name TEXT NOT NULL,
          tool_version TEXT NOT NULL,
          result_text TEXT NOT NULL,
          result_sha256 TEXT NOT NULL,
          confidence REAL,
          status TEXT NOT NULL CHECK(status IN ('success','review_required','failed')),
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_scans (
          run_id TEXT NOT NULL REFERENCES sync_runs(run_id),
          conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
          status TEXT NOT NULL CHECK(status IN ('complete','incomplete')),
          message_count INTEGER NOT NULL,
          error_code TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(run_id, conversation_id)
        );

        CREATE TABLE IF NOT EXISTS ai_batches (
          batch_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
          period_key TEXT NOT NULL,
          message_count INTEGER NOT NULL,
          character_count INTEGER NOT NULL,
          estimated_tokens INTEGER NOT NULL,
          content_sha256 TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('paused_budget','ready_for_ai','processing','review_required','complete','failed')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          last_error TEXT NOT NULL DEFAULT '',
          UNIQUE(conversation_id, period_key, content_sha256)
        );

        PRAGMA user_version=1;
        """
    )
    path.chmod(0o600)
    return connection


def open_plain_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise WeChatSyncError("snapshot_missing", "a required database snapshot is missing")
    uri = f"file:{quote(str(path))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise WeChatSyncError("schema_invalid", "unsafe table name in snapshot")
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info([{table}])")}


def get_wechat_pid() -> int:
    result = subprocess.run(
        ["/usr/bin/pgrep", "-x", "WeChat"], text=True, capture_output=True, check=False, timeout=10
    )
    pids = [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]
    if len(pids) != 1:
        raise WeChatSyncError("active_account_ambiguous", "exactly one main WeChat process is required")
    return pids[0]


def discover_active_account_root(pid: int) -> Path:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-Fn", "-p", str(pid)],
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    roots: set[Path] = set()
    marker = "/db_storage/"
    for line in result.stdout.splitlines():
        if not line.startswith("n") or "/xwechat_files/" not in line or marker not in line:
            continue
        path = line[1:]
        roots.add(Path(path.split(marker, 1)[0]))
    if len(roots) != 1:
        raise WeChatSyncError("active_account_ambiguous", "cannot resolve one active WeChat account root")
    root = next(iter(roots))
    if not (root / "db_storage").is_dir():
        raise WeChatSyncError("active_account_missing", "active WeChat database root is unavailable")
    return root


def app_receipt() -> dict[str, str]:
    app = Path("/Applications/WeChat.app")
    executable = app / "Contents" / "MacOS" / "WeChat"
    verify = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True,
        check=False,
        timeout=45,
    )
    if verify.returncode != 0:
        raise WeChatSyncError("wechat_signature_invalid", "WeChat code signature verification failed")
    signature = subprocess.run(
        ["/usr/bin/codesign", "-dv", "--verbose=4", str(app)],
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    signature_fields: dict[str, str] = {}
    for line in signature.stderr.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"Identifier", "TeamIdentifier", "CDHash"}:
            signature_fields[key] = value.strip()
    if signature.returncode != 0 or not all(
        signature_fields.get(field) for field in ("Identifier", "TeamIdentifier", "CDHash")
    ):
        raise WeChatSyncError("wechat_signature_invalid", "WeChat signature metadata is incomplete")
    version = subprocess.run(
        ["/usr/bin/defaults", "read", str(app / "Contents" / "Info"), "CFBundleShortVersionString"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    ).stdout.strip()
    build = subprocess.run(
        ["/usr/bin/defaults", "read", str(app / "Contents" / "Info"), "CFBundleVersion"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    ).stdout.strip()
    executable_hash = sha256_bytes(executable.read_bytes())
    return {
        "version": version,
        "build": build,
        "executable_sha256": executable_hash,
        "signature_identifier": signature_fields["Identifier"],
        "signature_team_identifier": signature_fields["TeamIdentifier"],
        "signature_cdhash": signature_fields["CDHash"],
    }


def strict_sip_state() -> str:
    result = subprocess.run(
        ["/usr/bin/csrutil", "status"], text=True, capture_output=True, check=False, timeout=10
    )
    output = result.stdout.casefold()
    if (
        result.returncode == 0
        and re.search(r"system integrity protection status:\s*enabled\s*\.", output)
        and "custom configuration" not in output
        and "disabled" not in output
    ):
        return "enabled"
    if re.search(r"system integrity protection status:\s*disabled\s*\.", output):
        return "disabled"
    return "unknown"


def require_sip_enabled() -> None:
    if strict_sip_state() != "enabled":
        raise WeChatSyncError("sip_not_enabled", "production collection is blocked until SIP is enabled")


def require_filevault_enabled() -> None:
    result = subprocess.run(
        ["/usr/bin/fdesetup", "status"], text=True, capture_output=True, check=False, timeout=20
    )
    if result.returncode != 0 or "filevault is on" not in result.stdout.casefold():
        raise WeChatSyncError("filevault_not_enabled", "production collection requires FileVault")


def read_enrollment(path: Path, account_fingerprint: str, receipt: dict[str, str]) -> dict[str, Any]:
    try:
        enrollment = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeChatSyncError("enrollment_required", "one-time WeChat key enrollment is required") from exc
    if enrollment.get("account_fingerprint") != account_fingerprint:
        raise WeChatSyncError("account_changed", "the active WeChat account differs from enrollment")
    if enrollment.get("production_ready") is not True:
        raise WeChatSyncError("enrollment_required", "enrollment has not been verified after SIP re-enable")
    for field in (
        "version", "build", "executable_sha256", "signature_identifier",
        "signature_team_identifier", "signature_cdhash",
    ):
        if enrollment.get("wechat", {}).get(field) != receipt[field]:
            raise WeChatSyncError("wechat_changed", "WeChat version or signature baseline changed")
    records = enrollment.get("records")
    if not isinstance(records, list) or not records:
        raise WeChatSyncError("enrollment_required", "enrollment contains no database key metadata")
    return enrollment


def keychain_fetch(helper: Path, key_id: str, expected_length: int = 32) -> bytes:
    result = subprocess.run(
        [str(helper), "fetch", "--key-id", key_id],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0 or len(result.stdout) != expected_length:
        raise WeChatSyncError("keychain_key_unavailable", "a required Keychain secret is unavailable")
    return result.stdout


def sql_quote(value: str) -> str:
    return value.replace("'", "''")


def snapshot_encrypted_database(
    source: Path,
    destination: Path,
    key: bytes,
    salt_hex: str,
    sqlcipher_bin: Path,
) -> None:
    ensure_private_dir(destination.parent)
    key_hex = key.hex()
    script = "\n".join(
        (
            f'PRAGMA key = "x\'{key_hex}{salt_hex}\'";',
            "PRAGMA cipher_page_size = 4096;",
            "PRAGMA cipher_hmac_algorithm = HMAC_SHA512;",
            "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;",
            "SELECT count(*) FROM sqlite_master;",
            f"ATTACH DATABASE '{sql_quote(str(destination))}' AS plaintext KEY '';",
            "SELECT sqlcipher_export('plaintext');",
            "DETACH DATABASE plaintext;",
        )
    )
    source_uri = f"file:{quote(str(source))}?mode=ro"
    result = subprocess.run(
        [str(sqlcipher_bin), "-batch", "-bail", source_uri],
        input=script.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=600,
        env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "LC_ALL": "C"},
    )
    key_buffer = bytearray(key)
    for index in range(len(key_buffer)):
        key_buffer[index] = 0
    if result.returncode != 0 or not destination.is_file():
        raise WeChatSyncError("sqlcipher_snapshot_failed", "encrypted database snapshot failed")
    destination.chmod(0o600)
    check = sqlite3.connect(destination)
    try:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise WeChatSyncError("snapshot_integrity_failed", "database snapshot quick_check failed")
    finally:
        check.close()


def require_sqlcipher_version(binary: Path, expected: str) -> None:
    result = subprocess.run(
        [str(binary), "--version"], text=True, capture_output=True, check=False, timeout=20
    )
    if result.returncode != 0 or f"SQLCipher {expected}" not in result.stdout:
        raise WeChatSyncError("sqlcipher_version_changed", "the pinned SQLCipher reader version is unavailable")


def readonly_file_signature(path: Path) -> tuple[int, int, int, int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise WeChatSyncError("source_database_invalid", "a WeChat database source is not a regular file")
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    finally:
        os.close(descriptor)


def copy_file_readonly(source: Path, destination: Path) -> None:
    ensure_private_dir(destination.parent)
    source_descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4()}.copy"
    output_descriptor = -1
    try:
        source_info = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_info.st_mode):
            raise WeChatSyncError("source_database_invalid", "a WeChat database source is not a regular file")
        output_descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(output_descriptor, view)
                view = view[written:]
        os.fsync(output_descriptor)
        os.close(output_descriptor)
        output_descriptor = -1
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        os.close(source_descriptor)
        if output_descriptor >= 0:
            os.close(output_descriptor)
        if temporary.exists():
            temporary.unlink()


def stable_readonly_copy(source: Path, destination: Path, attempts: int = 5) -> None:
    for _attempt in range(attempts):
        destination_wal = Path(str(destination) + "-wal")
        try:
            wal = Path(str(source) + "-wal")
            before_paths = [source] + ([wal] if wal.is_file() and not wal.is_symlink() else [])
            before = [(str(path), readonly_file_signature(path)) for path in before_paths]
            copy_file_readonly(source, destination)
            if len(before_paths) == 2:
                copy_file_readonly(wal, destination_wal)
            elif destination_wal.exists():
                destination_wal.unlink()
            after_paths = [source] + ([wal] if wal.is_file() and not wal.is_symlink() else [])
            after = [(str(path), readonly_file_signature(path)) for path in after_paths]
            if before == after:
                return
        except OSError:
            pass
        if destination.exists():
            destination.unlink()
        if destination_wal.exists():
            destination_wal.unlink()
    raise WeChatSyncError("source_snapshot_unstable", "WeChat database or WAL changed during read-only copy")


def target_database_paths(account_root: Path) -> list[Path]:
    storage = account_root / "db_storage"
    targets = [storage / "contact" / "contact.db", storage / "session" / "session.db"]
    message_dir = storage / "message"
    targets.extend(sorted(path for path in message_dir.glob("message_*.db") if MESSAGE_DB_RE.fullmatch(path.name)))
    resource = message_dir / "message_resource.db"
    if resource.is_file():
        targets.append(resource)
    targets.extend(sorted(path for path in message_dir.glob("media_*.db") if MEDIA_DB_RE.fullmatch(path.name)))
    if not targets[0].is_file() or len([path for path in targets if MESSAGE_DB_RE.fullmatch(path.name)]) < 1:
        raise WeChatSyncError("source_database_missing", "required active WeChat databases are unavailable")
    return targets


def prepare_production_snapshots(
    config: dict[str, Any], external_root: Path, account_root: Path, account_fingerprint: str
) -> Path:
    chat = config["wechat_chat"]
    enrollment_path = external_root / "wechat-chat" / "state" / "enrollment.json"
    receipt = app_receipt()
    enrollment = read_enrollment(enrollment_path, account_fingerprint, receipt)
    record_map = {str(item.get("relative_path")): item for item in enrollment["records"] if isinstance(item, dict)}
    targets = target_database_paths(account_root)
    relative_targets = [str(path.relative_to(account_root / "db_storage")) for path in targets]
    missing = [relative for relative in relative_targets if relative not in record_map]
    if missing:
        raise WeChatSyncError("key_rotation_required", "new or changed WeChat databases require supervised re-enrollment")

    run_root = external_root / "wechat-chat" / "work" / f"snapshot-{uuid.uuid4()}"
    ensure_private_dir(run_root)
    helper = Path(chat["keychain_helper"])
    sqlcipher_bin = Path(chat["sqlcipher_bin"])
    if not helper.is_file() or not sqlcipher_bin.is_file():
        raise WeChatSyncError("dependency_missing", "WeChat key or SQLCipher helper is missing")
    require_sqlcipher_version(sqlcipher_bin, str(chat["sqlcipher_expected_version"]))

    for source, relative in zip(targets, relative_targets):
        record = record_map[relative]
        salt_hex = str(record.get("salt_hex", ""))
        if not re.fullmatch(r"[0-9a-f]{32}", salt_hex):
            raise WeChatSyncError("enrollment_invalid", "enrollment salt metadata is invalid")
        key = keychain_fetch(helper, str(record.get("key_id", "")))
        destination = run_root / relative
        encrypted_copy = run_root / ".encrypted" / relative
        stable_readonly_copy(source, encrypted_copy)
        snapshot_encrypted_database(encrypted_copy, destination, key, salt_hex, sqlcipher_bin)
    return run_root


def contact_rows(contact_db: Path) -> tuple[list[dict[str, Any]], dict[str, str], set[int]]:
    connection = open_plain_readonly(contact_db)
    try:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "contact" not in tables or "contact_label" not in tables:
            raise WeChatSyncError("contact_schema_unsupported", "WeChat contact label schema is unsupported")
        label_rows = connection.execute(
            "SELECT label_id_,label_name_ FROM contact_label WHERE label_name_=?", ("示例企业",)
        ).fetchall()
        label_ids = {int(row[0]) for row in label_rows}
        if not label_ids:
            raise WeChatSyncError("tag_missing", "the exact WeChat tag 示例企业 was not found")
        columns = table_columns(connection, "contact")
        required = {"username", "nick_name", "remark", "extra_buffer"}
        if not required.issubset(columns):
            raise WeChatSyncError("contact_schema_unsupported", "required contact fields are unavailable")
        selected = ["username", "nick_name", "remark", "extra_buffer"]
        if "local_type" in columns:
            selected.append("local_type")
        query = "SELECT " + ",".join(f"[{column}]" for column in selected) + " FROM contact"
        rows: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        for row in connection.execute(query):
            data = dict(zip(selected, row))
            username = str(data.get("username") or "")
            if not username:
                continue
            display = str(data.get("remark") or data.get("nick_name") or "未命名会话")
            names[username] = display
            rows.append({**data, "username": username, "display_name": display})
        return rows, names, label_ids
    finally:
        connection.close()


def discover_scope(snapshot_root: Path, config: dict[str, Any], account_fingerprint: str) -> list[dict[str, str]]:
    rows, _names, label_ids = contact_rows(snapshot_root / "contact" / "contact.db")
    chat = config["wechat_chat"]
    aliases = [str(item) for item in chat.get("group_name_aliases", [])]
    excludes = [str(item) for item in chat.get("group_name_excludes", [])]
    scope: list[dict[str, str]] = []
    direct_count = 0
    for row in rows:
        username = row["username"]
        display = row["display_name"]
        if username.endswith("@chatroom"):
            if group_matches(display, aliases, excludes):
                scope.append(
                    {
                        "username": username,
                        "display_name": display,
                        "kind": "group",
                        "reason": "group_name_alias",
                        "conversation_id": stable_hash(account_fingerprint, username),
                    }
                )
            continue
        label_string = extract_pb_field_30(row.get("extra_buffer"))
        if not label_string:
            continue
        member_labels: set[int] = set()
        for item in label_string.split(","):
            try:
                member_labels.add(int(item.strip()))
            except ValueError:
                continue
        if member_labels.intersection(label_ids):
            direct_count += 1
            scope.append(
                {
                    "username": username,
                    "display_name": display,
                    "kind": "direct",
                    "reason": "exact_tag:示例企业",
                    "conversation_id": stable_hash(account_fingerprint, username),
                }
            )
    if direct_count == 0:
        raise WeChatSyncError("tag_empty", "the exact WeChat tag 示例企业 has no direct-chat members")
    unique = {item["conversation_id"]: item for item in scope}
    return sorted(unique.values(), key=lambda item: (item["kind"], item["display_name"].casefold()))


def list_message_shards(snapshot_root: Path) -> list[Path]:
    message_dir = snapshot_root / "message"
    return sorted(path for path in message_dir.glob("message_*.db") if MESSAGE_DB_RE.fullmatch(path.name))


def message_count_for(scope_item: dict[str, str], shards: list[Path], cutoff: int) -> int:
    table = "Msg_" + hashlib.md5(scope_item["username"].encode("utf-8")).hexdigest()
    total = 0
    for shard in shards:
        connection = open_plain_readonly(shard)
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                total += int(connection.execute(f"SELECT COUNT(*) FROM [{table}] WHERE create_time>=?", (cutoff,)).fetchone()[0])
        finally:
            connection.close()
    return total


def select_pilot_scope(scope: list[dict[str, str]], shards: list[Path], days: int) -> tuple[list[dict[str, str]], int]:
    cutoff = int((datetime.now().astimezone() - timedelta(days=days)).timestamp())
    selected: list[dict[str, str]] = []
    for kind in ("direct", "group"):
        candidates: list[tuple[int, dict[str, str]]] = []
        for item in scope:
            if item["kind"] != kind:
                continue
            count = message_count_for(item, shards, cutoff)
            if count > 0:
                candidates.append((count, item))
        if candidates:
            candidates.sort(key=lambda pair: (pair[0], pair[1]["conversation_id"]))
            selected.append(candidates[0][1])
    if not any(item["kind"] == "direct" for item in selected):
        raise WeChatSyncError("pilot_no_direct_messages", "no tagged direct chat has messages in the pilot window")
    if not any(item["kind"] == "group" for item in selected):
        raise WeChatSyncError("pilot_no_group_messages", "no matched group chat has messages in the pilot window")
    return selected, cutoff


def write_scope_receipts(external_root: Path, scope: list[dict[str, str]], account_fingerprint: str) -> None:
    state = external_root / "wechat-chat" / "state"
    ensure_private_dir(state)
    local_manifest = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "account_fingerprint": account_fingerprint,
        "scope": [
            {
                "conversation_id": item["conversation_id"],
                "kind": item["kind"],
                "display_name": item["display_name"],
                "match_reason": item["reason"],
            }
            for item in scope
        ],
    }
    counts = defaultdict(int)
    for item in scope:
        counts[item["kind"]] += 1
    receipt = {
        "schema_version": 1,
        "generated_at": local_manifest["generated_at"],
        "account_fingerprint": account_fingerprint,
        "direct_count": counts["direct"],
        "group_count": counts["group"],
        "scope_sha256": sha256_bytes(canonical_json(local_manifest["scope"]).encode("utf-8")),
    }
    atomic_write(state / "scope_manifest.json", json.dumps(local_manifest, ensure_ascii=False, indent=2) + "\n")
    atomic_write(state / "scope_receipt.json", json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")


def scope_expansion_guard(
    connection: sqlite3.Connection,
    scope: list[dict[str, str]],
    absolute_limit: int = 20,
    percentage_limit: float = 0.25,
) -> None:
    previous = {
        str(row[0]) for row in connection.execute("SELECT conversation_id FROM scope_members WHERE active=1")
    }
    if not previous:
        return
    current = {item["conversation_id"] for item in scope}
    added = len(current - previous)
    if added > max(0, absolute_limit) or (added / len(previous)) > max(0.0, percentage_limit):
        raise WeChatSyncError("scope_expansion_paused", "scope expanded beyond the approved safety threshold")


def upsert_scope(
    connection: sqlite3.Connection, scope: list[dict[str, str]], account_fingerprint: str, observed_at: str
) -> None:
    active_ids = {item["conversation_id"] for item in scope}
    connection.execute("UPDATE scope_members SET active=0 WHERE active=1")
    for item in scope:
        safe_name, _ = redact_text(item["display_name"], max_chars=300)
        connection.execute(
            """
            INSERT INTO scope_members(
              conversation_id,account_fingerprint,source_username_sha256,conversation_kind,
              display_name,match_reason,active,first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(conversation_id) DO UPDATE SET
              display_name=excluded.display_name,match_reason=excluded.match_reason,
              conversation_kind=excluded.conversation_kind,active=1,last_seen_at=excluded.last_seen_at
            """,
            (
                item["conversation_id"],
                account_fingerprint,
                sha256_bytes(item["username"].encode("utf-8")),
                item["kind"],
                safe_name,
                item["reason"],
                1,
                observed_at,
                observed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO conversations(
              conversation_id,account_fingerprint,conversation_kind,display_name,current_visible
            ) VALUES(?,?,?,?,1)
            ON CONFLICT(conversation_id) DO UPDATE SET
              conversation_kind=excluded.conversation_kind,display_name=excluded.display_name,current_visible=1
            """,
            (item["conversation_id"], account_fingerprint, item["kind"], safe_name),
        )
    if active_ids:
        placeholders = ",".join("?" for _ in active_ids)
        connection.execute(
            f"UPDATE conversations SET current_visible=0 WHERE conversation_id NOT IN ({placeholders})",
            tuple(active_ids),
        )


def load_name2id(connection: sqlite3.Connection) -> dict[Any, str]:
    try:
        return {
            row[0]: str(row[1])
            for row in connection.execute("SELECT rowid,user_name FROM Name2Id")
            if row[1]
        }
    except sqlite3.Error:
        return {}


def open_media_shards(snapshot_root: Path) -> list[sqlite3.Connection]:
    connections: list[sqlite3.Connection] = []
    for path in sorted((snapshot_root / "message").glob("media_*.db")):
        if not MEDIA_DB_RE.fullmatch(path.name):
            continue
        try:
            connection = open_plain_readonly(path)
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if {"Name2Id", "VoiceInfo"}.issubset(tables):
                connections.append(connection)
            else:
                connection.close()
        except (sqlite3.Error, WeChatSyncError):
            continue
    return connections


def voice_blob(
    connections: list[sqlite3.Connection], username: str, local_id: Any
) -> bytes | None:
    for connection in connections:
        try:
            chat = connection.execute(
                "SELECT rowid FROM Name2Id WHERE user_name=?", (username,)
            ).fetchone()
            if not chat:
                continue
            row = connection.execute(
                "SELECT voice_data FROM VoiceInfo WHERE chat_name_id=? AND local_id=? "
                "ORDER BY create_time DESC LIMIT 1",
                (chat[0], local_id),
            ).fetchone()
            if row and isinstance(row[0], (bytes, bytearray, memoryview)):
                payload = bytes(row[0])
                if 0 < len(payload) <= 128 * 1024 * 1024:
                    return payload
        except sqlite3.Error:
            continue
    return None


def resource_digest(
    connection: sqlite3.Connection | None, username: str, local_id: Any, local_type: Any
) -> str:
    if connection is None or classify_message(local_type) != "image":
        return ""
    try:
        chat = connection.execute(
            "SELECT rowid FROM ChatName2Id WHERE user_name=?", (username,)
        ).fetchone()
        if not chat:
            return ""
        row = connection.execute(
            """
            SELECT packed_info FROM MessageResourceInfo
            WHERE chat_id=? AND message_local_id=?
              AND (message_local_type=3 OR message_local_type % 4294967296=3)
            ORDER BY message_create_time DESC LIMIT 1
            """,
            (chat[0], local_id),
        ).fetchone()
        packed = row[0] if row else None
        if not isinstance(packed, bytes):
            return ""
        marker = b"\x12\x22\x0a\x20"
        position = packed.find(marker)
        if position >= 0 and position + len(marker) + 32 <= len(packed):
            candidate = packed[position + len(marker):position + len(marker) + 32].decode("ascii")
            if re.fullmatch(r"[0-9a-f]{32}", candidate):
                return candidate
        match = re.search(rb"[0-9a-f]{32}", packed)
        return match.group(0).decode("ascii") if match else ""
    except (sqlite3.Error, UnicodeDecodeError):
        return ""


def message_rows(connection: sqlite3.Connection, table: str, cutoff: int) -> Iterable[sqlite3.Row]:
    if not re.fullmatch(r"Msg_[0-9a-f]{32}", table):
        raise WeChatSyncError("message_schema_unsupported", "unsafe message table name")
    columns = table_columns(connection, table)
    required = {"local_id", "local_type", "create_time", "real_sender_id", "message_content"}
    if not required.issubset(columns):
        raise WeChatSyncError("message_schema_unsupported", "required WeChat message fields are unavailable")
    selected = ["local_id", "local_type", "create_time", "real_sender_id", "message_content"]
    for optional in ("server_id", "WCDB_CT_message_content"):
        if optional in columns:
            selected.append(optional)
    query = (
        "SELECT " + ",".join(f"[{column}]" for column in selected)
        + f" FROM [{table}] WHERE create_time>=? ORDER BY create_time ASC"
    )
    for row in connection.execute(query, (cutoff,)):
        yield row


def safe_sender(
    real_sender_id: Any,
    sender_hint: str,
    id_to_username: dict[Any, str],
    names: dict[str, str],
    scope_item: dict[str, str],
) -> tuple[str, str]:
    username = id_to_username.get(real_sender_id, "") or sender_hint
    if username:
        name = names.get(username, "群成员" if scope_item["kind"] == "group" else scope_item["display_name"])
        safe_name, _ = redact_text(name, max_chars=200)
        return safe_name, sha256_bytes(username.encode("utf-8"))
    return ("未知成员" if scope_item["kind"] == "group" else "本人或对方"), ""


def upsert_message(
    connection: sqlite3.Connection,
    scope_item: dict[str, str],
    shard_name: str,
    row: sqlite3.Row,
    names: dict[str, str],
    id_to_username: dict[Any, str],
    observed_at: str,
    force_sensitive_reply: bool,
    resolved_resource_digest: str,
) -> tuple[str, bool, bool, int, bool, str]:
    values = dict(row)
    local_id = str(values.get("local_id") or "")
    create_time = int(values.get("create_time") or 0)
    server_id = str(values.get("server_id") or "")
    local_type = values.get("local_type")
    raw_content = values.get("message_content")
    sender_hint, rendered, metadata = render_message_content(
        local_type, raw_content, scope_item["kind"] == "group"
    )
    compressed_present = bool(values.get("WCDB_CT_message_content"))
    if not rendered and compressed_present:
        rendered = "[压缩内容待兼容解码]"
        metadata["compressed_content_pending"] = True
    if force_sensitive_reply and rendered.strip():
        rendered = "[敏感回复已脱敏]"
        metadata["redaction_count"] = int(metadata.get("redaction_count", 0)) + 1
    if resolved_resource_digest and "content_digest" not in metadata:
        metadata["content_digest"] = resolved_resource_digest
    redactions = int(metadata.get("redaction_count", 0))
    sender_name, sender_hash = safe_sender(
        values.get("real_sender_id"), sender_hint, id_to_username, names, scope_item
    )
    structured_json = canonical_json({key: value for key, value in metadata.items() if key != "redaction_count"})
    content_hash = stable_hash(
        scope_item["conversation_id"], create_time, sender_hash, classify_message(local_type), rendered, structured_json
    )
    raw_text = raw_content if isinstance(raw_content, str) else ""
    original_hash = "" if redactions else sha256_bytes(raw_text.encode("utf-8"))
    message_id = (
        stable_hash(scope_item["conversation_id"], "server", server_id)
        if server_id and server_id != "0"
        else stable_hash(scope_item["conversation_id"], shard_name, local_id, create_time)
    )
    existing = connection.execute(
        "SELECT content_sha256 FROM messages WHERE message_id=?", (message_id,)
    ).fetchone()
    inserted = existing is None
    updated = existing is not None and str(existing[0]) != content_hash
    connection.execute(
        """
        INSERT INTO messages(
          message_id,conversation_id,create_time,source_shard,source_local_id,
          source_server_id_sha256,sender_name,sender_id_sha256,message_type,text_redacted,
          structured_json,content_sha256,original_text_sha256,redaction_count,current_visible,
          first_seen_at,last_seen_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
        ON CONFLICT(message_id) DO UPDATE SET
          create_time=excluded.create_time,source_shard=excluded.source_shard,
          sender_name=excluded.sender_name,sender_id_sha256=excluded.sender_id_sha256,
          message_type=excluded.message_type,text_redacted=excluded.text_redacted,
          structured_json=excluded.structured_json,content_sha256=excluded.content_sha256,
          original_text_sha256=excluded.original_text_sha256,redaction_count=excluded.redaction_count,
          current_visible=1,last_seen_at=excluded.last_seen_at
        """,
        (
            message_id,
            scope_item["conversation_id"],
            create_time,
            shard_name,
            local_id,
            sha256_bytes(server_id.encode("utf-8")) if server_id and server_id != "0" else "",
            sender_name,
            sender_hash,
            classify_message(local_type),
            rendered,
            structured_json,
            content_hash,
            original_hash,
            redactions,
            observed_at,
            observed_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO message_versions(
          message_id,content_sha256,observed_at,text_redacted,structured_json,
          original_text_sha256,redaction_count
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(message_id,content_sha256) DO NOTHING
        """,
        (message_id, content_hash, observed_at, rendered, structured_json, original_hash, redactions),
    )

    media_type = classify_message(local_type)
    queued = media_type in {"image", "voice", "video", "emoji"} or (
        media_type == "app" and bool(
            metadata.get("content_digest") or metadata.get("attachment_name_sha256")
        )
    )
    asset_id = ""
    if queued:
        locator = {
            "source_shard": shard_name,
            "source_local_id": local_id,
            "create_time": create_time,
            "message_type": media_type,
            "chat_path_hash": hashlib.md5(scope_item["username"].encode("utf-8")).hexdigest(),
            **{key: value for key, value in metadata.items() if key != "redaction_count"},
        }
        locator_json = canonical_json(locator)
        asset_id = stable_hash(message_id, media_type)
        connection.execute(
            """
            INSERT INTO media_assets(
              asset_id,message_id,media_type,locator_json,locator_sha256,status,
              first_seen_at,updated_at
            ) VALUES(?,?,?,?,?,'queued',?,?)
            ON CONFLICT(asset_id) DO UPDATE SET
              locator_json=excluded.locator_json,locator_sha256=excluded.locator_sha256,
              updated_at=excluded.updated_at,
              status=CASE WHEN media_assets.status IN ('processed','quarantined')
                          THEN media_assets.status ELSE 'queued' END
            """,
            (
                asset_id,
                message_id,
                media_type,
                locator_json,
                sha256_bytes(locator_json.encode("utf-8")),
                observed_at,
                observed_at,
            ),
        )
    return message_id, inserted, updated, redactions, queued, asset_id


def import_scope_messages(
    connection: sqlite3.Connection,
    snapshot_root: Path,
    source_root: Path,
    scope: list[dict[str, str]],
    cutoff: int,
    run_id: str,
    observed_at: str,
) -> dict[str, int]:
    rows, names, _label_ids = contact_rows(snapshot_root / "contact" / "contact.db")
    del rows
    shards = list_message_shards(snapshot_root)
    if not shards:
        raise WeChatSyncError("message_snapshot_missing", "no message shard snapshots are available")
    stats = defaultdict(int)
    resource_connection: sqlite3.Connection | None = None
    resource_path = snapshot_root / "message" / "message_resource.db"
    if resource_path.is_file():
        try:
            resource_connection = open_plain_readonly(resource_path)
        except WeChatSyncError:
            resource_connection = None
    media_connections = open_media_shards(snapshot_root)
    for scope_item in scope:
        table = "Msg_" + hashlib.md5(scope_item["username"].encode("utf-8")).hexdigest()
        seen: set[str] = set()
        scan_complete = True
        error_code = ""
        previous_sensitive_prompt = False
        for shard in shards:
            shard_connection = open_plain_readonly(shard)
            try:
                exists = shard_connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
                id_to_username = load_name2id(shard_connection)
                for row in message_rows(shard_connection, table, cutoff):
                    values = dict(row)
                    message_id, inserted, updated, redactions, queued, asset_id = upsert_message(
                        connection,
                        scope_item,
                        shard.name,
                        row,
                        names,
                        id_to_username,
                        observed_at,
                        previous_sensitive_prompt,
                        resource_digest(
                            resource_connection,
                            scope_item["username"],
                            values.get("local_id"),
                            values.get("local_type"),
                        ),
                    )
                    seen.add(message_id)
                    stats["messages_seen"] += 1
                    stats["messages_inserted"] += int(inserted)
                    stats["messages_updated"] += int(updated)
                    stats["redaction_count"] += redactions
                    stats["media_queued"] += int(queued)
                    if classify_message(values.get("local_type")) == "voice" and asset_id:
                        payload = voice_blob(
                            media_connections, scope_item["username"], values.get("local_id")
                        )
                        if payload is not None:
                            voice_path = source_root / "media" / asset_id / "source.silk"
                            atomic_write_bytes(voice_path, payload)
                            connection.execute(
                                """
                                UPDATE media_assets SET local_path=?,content_sha256=?,byte_size=?,
                                  status='resolved',error_code='',updated_at=? WHERE asset_id=?
                                """,
                                (
                                    str(voice_path),
                                    sha256_bytes(payload),
                                    len(payload),
                                    observed_at,
                                    asset_id,
                                ),
                            )
                    text_row = connection.execute(
                        "SELECT text_redacted FROM messages WHERE message_id=?", (message_id,)
                    ).fetchone()
                    previous_sensitive_prompt = bool(text_row and SENSITIVE_PROMPT_RE.search(str(text_row[0])))
            except (sqlite3.Error, WeChatSyncError):
                scan_complete = False
                error_code = "message_scan_failed"
            finally:
                shard_connection.close()

        if scan_complete:
            existing_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT message_id FROM messages WHERE conversation_id=? AND create_time>=? AND current_visible=1",
                    (scope_item["conversation_id"], cutoff),
                )
            }
            missing = existing_ids - seen
            for message_id in missing:
                connection.execute("UPDATE messages SET current_visible=0,last_seen_at=? WHERE message_id=?", (observed_at, message_id))
            stats["conversations_complete"] += 1
        else:
            stats["conversations_incomplete"] += 1
        bounds = connection.execute(
            "SELECT MIN(create_time),MAX(create_time) FROM messages WHERE conversation_id=? AND current_visible=1",
            (scope_item["conversation_id"],),
        ).fetchone()
        connection.execute(
            """
            UPDATE conversations SET first_message_at=?,last_message_at=?,latest_scan_id=?,scan_complete=?
            WHERE conversation_id=?
            """,
            (bounds[0], bounds[1], run_id, int(scan_complete), scope_item["conversation_id"]),
        )
        connection.execute(
            "INSERT INTO conversation_scans(run_id,conversation_id,status,message_count,error_code) VALUES(?,?,?,?,?)",
            (
                run_id,
                scope_item["conversation_id"],
                "complete" if scan_complete else "incomplete",
                len(seen),
                error_code,
            ),
        )
    if resource_connection is not None:
        resource_connection.close()
    for media_connection in media_connections:
        media_connection.close()
    return dict(stats)


def refresh_ai_batches(connection: sqlite3.Connection, approved_tokens: int, observed_at: str) -> dict[str, int]:
    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT message_id,conversation_id,create_time,sender_name,message_type,text_redacted,
               structured_json,content_sha256
        FROM messages WHERE current_visible=1 ORDER BY conversation_id,create_time
        """
    ):
        period = datetime.fromtimestamp(int(row["create_time"])).astimezone().strftime("%Y-%m")
        groups[(str(row["conversation_id"]), period)].append(row)
    derivations: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT m.conversation_id,m.create_time,d.derivation_type,d.result_text,
               d.result_sha256,d.confidence,d.status
        FROM media_derivations d
        JOIN media_assets a ON a.asset_id=d.asset_id
        JOIN messages m ON m.message_id=a.message_id
        WHERE m.current_visible=1
        ORDER BY m.conversation_id,m.create_time,d.derivation_type,d.derivation_id
        """
    ):
        period = datetime.fromtimestamp(int(row["create_time"])).astimezone().strftime("%Y-%m")
        derivations[(str(row["conversation_id"]), period)].append(row)
    consumed_tokens = int(
        connection.execute(
            "SELECT COALESCE(SUM(estimated_tokens),0) FROM ai_batches WHERE status='complete'"
        ).fetchone()[0]
    )
    processing_tokens = int(
        connection.execute(
            "SELECT COALESCE(SUM(estimated_tokens),0) FROM ai_batches WHERE status='processing'"
        ).fetchone()[0]
    )
    available_tokens = max(0, approved_tokens - consumed_tokens - processing_tokens)
    total_tokens = 0
    ready_tokens = 0
    for (conversation_id, period), rows in groups.items():
        lines = [
            f"{row['create_time']}|{row['sender_name']}|{row['message_type']}|{row['text_redacted']}|{row['structured_json']}"
            for row in rows
        ]
        derivative_rows = derivations.get((conversation_id, period), [])
        lines.extend(
            f"derived|{row['derivation_type']}|confidence={row['confidence']}|status={row['status']}|{row['result_text']}"
            for row in derivative_rows
        )
        joined = "\n".join(lines)
        tokens = estimate_tokens(joined)
        total_tokens += tokens
        content_hash = sha256_bytes(
            "\n".join(
                [str(row["content_sha256"]) for row in rows]
                + [str(row["result_sha256"]) for row in derivative_rows]
            ).encode("utf-8")
        )
        existing_status_row = connection.execute(
            "SELECT status FROM ai_batches WHERE conversation_id=? AND period_key=? AND content_sha256=?",
            (conversation_id, period, content_hash),
        ).fetchone()
        existing_status = str(existing_status_row[0]) if existing_status_row else ""
        status = existing_status if existing_status in {"processing", "complete"} else "paused_budget"
        if status not in {"processing", "complete"} and available_tokens > 0 and ready_tokens + tokens <= available_tokens:
            status = "ready_for_ai"
            ready_tokens += tokens
        batch_id = stable_hash(conversation_id, period, content_hash)
        connection.execute(
            """
            INSERT INTO ai_batches(
              batch_id,conversation_id,period_key,message_count,character_count,
              estimated_tokens,content_sha256,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(conversation_id,period_key,content_sha256) DO UPDATE SET
              message_count=excluded.message_count,character_count=excluded.character_count,
              estimated_tokens=excluded.estimated_tokens,
              status=CASE WHEN ai_batches.status IN ('processing','complete')
                          THEN ai_batches.status ELSE excluded.status END,
              updated_at=excluded.updated_at
            """,
            (
                batch_id,
                conversation_id,
                period,
                len(rows),
                len(joined),
                tokens,
                content_hash,
                status,
                observed_at,
                observed_at,
            ),
        )
        connection.execute(
            """
            UPDATE ai_batches SET status='failed',last_error='superseded_by_new_content_hash',updated_at=?
            WHERE conversation_id=? AND period_key=? AND content_sha256<>?
              AND status NOT IN ('processing','complete')
            """,
            (observed_at, conversation_id, period, content_hash),
        )
    return {
        "estimated_tokens": total_tokens,
        "ready_tokens": ready_tokens,
        "approved_tokens": approved_tokens,
        "consumed_tokens": consumed_tokens,
        "processing_tokens": processing_tokens,
        "remaining_tokens": max(
            0, approved_tokens - consumed_tokens - processing_tokens - ready_tokens
        ),
    }


def write_status(external_root: Path, status: dict[str, Any]) -> None:
    if status.get("status") not in RUN_STATES:
        raise ValueError("invalid status receipt")
    safe = {
        key: value
        for key, value in status.items()
        if key not in {"account_root", "conversation_id", "display_name", "username", "text", "content", "key"}
    }
    atomic_write(
        external_root / "wechat-chat" / "state" / "status.json",
        json.dumps(safe, ensure_ascii=False, indent=2) + "\n",
    )


def run_collection(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = load_config(args.config)
    chat = config["wechat_chat"]
    external_root = Path(config["external_root"])
    source_root = external_root / "wechat-chat"
    ensure_private_dir(source_root)
    for child in ("state", "logs", "work", "media", "quarantine"):
        ensure_private_dir(source_root / child)
    store_path = source_root / "state" / "chat.sqlite"
    connection = connect_store(store_path)
    run_id = str(uuid.uuid4())
    started = now_iso()
    mode = args.mode or str(chat.get("mode", "pilot"))
    if args.preflight_only:
        mode = "preflight"

    snapshot_root: Path | None = None
    production_snapshot = False
    account_fingerprint = "fixture"
    try:
        if args.fixture_root:
            snapshot_root = args.fixture_root.resolve()
            account_fingerprint = "fixture-" + sha256_bytes(str(snapshot_root).encode("utf-8"))[:16]
        else:
            require_sip_enabled()
            require_filevault_enabled()
            pid = get_wechat_pid()
            account_root = discover_active_account_root(pid)
            account_fingerprint = sha256_bytes(account_root.name.encode("utf-8"))
            snapshot_root = prepare_production_snapshots(config, external_root, account_root, account_fingerprint)
            production_snapshot = True

        cutoff = int(datetime.fromisoformat(str(chat["history_start"])).timestamp())
        connection.execute(
            "INSERT INTO sync_runs(run_id,started_at,status,mode,account_fingerprint,cutoff_epoch) VALUES(?,?,?,?,?,?)",
            (run_id, started, "collecting", mode, account_fingerprint, cutoff),
        )
        connection.commit()

        scope = discover_scope(snapshot_root, config, account_fingerprint)
        scope_expansion_guard(
            connection,
            scope,
            int(chat.get("scope_growth_absolute_limit", 20)),
            float(chat.get("scope_growth_percentage_limit", 0.25)),
        )
        write_scope_receipts(external_root, scope, account_fingerprint)
        observed = now_iso()
        upsert_scope(connection, scope, account_fingerprint, observed)
        direct_count = sum(item["kind"] == "direct" for item in scope)
        group_count = sum(item["kind"] == "group" for item in scope)
        connection.execute(
            "UPDATE sync_runs SET direct_count=?,group_count=? WHERE run_id=?",
            (direct_count, group_count, run_id),
        )
        if mode == "preflight":
            status = {
                "schema_version": 1,
                "status": "paused_budget",
                "run_id": run_id,
                "finished_at": now_iso(),
                "mode": mode,
                "account_fingerprint": account_fingerprint,
                "direct_count": direct_count,
                "group_count": group_count,
                "database_quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
                "production_snapshot": production_snapshot,
                "detail": "scope_manifest_ready",
            }
            connection.execute(
                "UPDATE sync_runs SET finished_at=?,status='paused_budget',detail='scope_manifest_ready' WHERE run_id=?",
                (status["finished_at"], run_id),
            )
            connection.commit()
            write_status(external_root, status)
            return status, 4

        selected_scope = scope
        active_cutoff = cutoff
        if mode == "pilot":
            selected_scope, active_cutoff = select_pilot_scope(
                scope, list_message_shards(snapshot_root), int(chat.get("pilot_days", 7))
            )
        stats = import_scope_messages(
            connection, snapshot_root, source_root, selected_scope, active_cutoff, run_id, observed
        )
        ai_stats = refresh_ai_batches(
            connection, int(chat.get("approved_ai_input_tokens", 0)), observed
        )
        has_incomplete = stats.get("conversations_incomplete", 0) > 0
        pending_media = connection.execute(
            "SELECT COUNT(*) FROM media_assets WHERE status='queued' AND COALESCE(error_code,'') NOT IN ('video_waiting_instruction','media_type_waiting_confirmation')"
        ).fetchone()[0]
        approved_tokens = int(chat.get("approved_ai_input_tokens", 0))
        if has_incomplete:
            final_status = "failed"
            exit_code = 2
        elif approved_tokens <= 0:
            final_status = "paused_budget"
            exit_code = 4
        elif pending_media:
            final_status = "media_pending"
            exit_code = 0
        else:
            final_status = "ready_for_ai"
            exit_code = 0
        finished = now_iso()
        connection.execute(
            """
            UPDATE sync_runs SET finished_at=?,status=?,cutoff_epoch=?,conversations_complete=?,
              conversations_incomplete=?,messages_seen=?,messages_inserted=?,messages_updated=?,
              redaction_count=?,media_queued=?,estimated_tokens=?,error_code=?,detail=?
            WHERE run_id=?
            """,
            (
                finished,
                final_status,
                active_cutoff,
                stats.get("conversations_complete", 0),
                stats.get("conversations_incomplete", 0),
                stats.get("messages_seen", 0),
                stats.get("messages_inserted", 0),
                stats.get("messages_updated", 0),
                stats.get("redaction_count", 0),
                stats.get("media_queued", 0),
                ai_stats["estimated_tokens"],
                "message_scan_incomplete" if has_incomplete else "",
                "pilot_cost_approval_required" if approved_tokens <= 0 else "",
                run_id,
            ),
        )
        connection.commit()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        status = {
            "schema_version": 1,
            "status": final_status,
            "run_id": run_id,
            "started_at": started,
            "finished_at": finished,
            "mode": mode,
            "account_fingerprint": account_fingerprint,
            "history_start": chat["history_start"],
            "active_cutoff_epoch": active_cutoff,
            "direct_count": direct_count,
            "group_count": group_count,
            "selected_conversations": len(selected_scope),
            **stats,
            **ai_stats,
            "media_pending": pending_media,
            "database_quick_check": quick_check,
            "database_user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "production_snapshot": production_snapshot,
            "detail": "pilot_cost_approval_required" if approved_tokens <= 0 else "",
        }
        write_status(external_root, status)
        append_metadata_log(
            source_root / "logs" / "collect.jsonl",
            "collection_finished",
            status=final_status,
            mode=mode,
            messages_seen=stats.get("messages_seen", 0),
            error_code=status["detail"],
        )
        return status, exit_code
    except WeChatSyncError as exc:
        final_state = "enrollment_required" if exc.code in {
            "enrollment_required", "account_changed", "keychain_key_unavailable", "key_rotation_required"
        } else "failed"
        status = {
            "schema_version": 1,
            "status": final_state,
            "run_id": run_id,
            "started_at": started,
            "finished_at": now_iso(),
            "mode": mode,
            "account_fingerprint": account_fingerprint,
            "error_code": exc.code,
            "detail": exc.message,
            "database_quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
        }
        try:
            connection.execute(
                """
                INSERT INTO sync_runs(
                  run_id,started_at,finished_at,status,mode,account_fingerprint,cutoff_epoch,error_code,detail
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                  finished_at=excluded.finished_at,status=excluded.status,
                  error_code=excluded.error_code,detail=excluded.detail
                """,
                (
                    run_id,
                    started,
                    status["finished_at"],
                    final_state,
                    mode,
                    account_fingerprint,
                    0,
                    exc.code,
                    exc.message,
                ),
            )
            connection.commit()
        finally:
            write_status(external_root, status)
            append_metadata_log(
                source_root / "logs" / "collect.jsonl",
                "collection_stopped",
                status=final_state,
                error_code=exc.code,
            )
        return status, 3 if final_state == "enrollment_required" else 2
    finally:
        connection.close()
        if production_snapshot and snapshot_root and snapshot_root.is_dir() and not args.keep_working:
            work_root = (external_root / "wechat-chat" / "work").resolve()
            resolved_snapshot = snapshot_root.resolve()
            if resolved_snapshot.parent == work_root and resolved_snapshot.name.startswith("snapshot-"):
                shutil.rmtree(resolved_snapshot)


def main() -> int:
    args = parse_arguments()
    lock = None
    try:
        lock = acquire_lock()
        result, exit_code = run_collection(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return exit_code
    except WeChatSyncError as exc:
        print(json.dumps({"status": "failed", "error_code": exc.code, "detail": exc.message}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "internal_error", "detail": "collection stopped safely"}, ensure_ascii=False))
        return 2
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
