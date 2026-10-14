#!/usr/bin/env python3
"""Safely hand redacted WeChat batches to Codex and control their state."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from wechat_common import WeChatSyncError, now_iso, sha256_bytes


os.umask(0o077)
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"
LOCK = ROOT / "state" / "wechat_ai_queue.lock"
BATCH_ID_RE = re.compile(r"[0-9a-f]{64}")
ERROR_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("claim-next")
    complete = subparsers.add_parser("complete")
    complete.add_argument("--batch-id", required=True)
    complete.add_argument("--validation-passed", action="store_true")
    for command in ("review", "fail"):
        state = subparsers.add_parser(command)
        state.add_argument("--batch-id", required=True)
        state.add_argument("--error-code", required=True)
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeChatSyncError("invalid_config", "cannot read WeChat AI queue configuration") from exc
    if not isinstance(config.get("wechat_chat"), dict):
        raise WeChatSyncError("invalid_config", "wechat_chat config is missing")
    return config


def acquire_lock() -> Any:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.parent.chmod(0o700)
    handle = LOCK.open("a+")
    LOCK.chmod(0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise WeChatSyncError("already_running", "another WeChat AI queue operation is active") from exc
    return handle


def connect_store(config: dict[str, Any]) -> sqlite3.Connection:
    database = Path(config["external_root"]) / "wechat-chat" / "state" / "chat.sqlite"
    if not database.is_file():
        raise WeChatSyncError("source_store_missing", "WeChat source database is unavailable")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        connection.close()
        raise WeChatSyncError("database_integrity_failed", "WeChat source database quick_check failed")
    return connection


def budget_receipt(connection: sqlite3.Connection, approved: int) -> dict[str, int]:
    counts = {
        str(row[0]): int(row[1])
        for row in connection.execute("SELECT status,COUNT(*) FROM ai_batches GROUP BY status")
    }
    tokens = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT status,COALESCE(SUM(estimated_tokens),0) FROM ai_batches GROUP BY status"
        )
    }
    consumed = tokens.get("complete", 0)
    processing = tokens.get("processing", 0)
    return {
        "approved_tokens": max(0, approved),
        "consumed_tokens": consumed,
        "processing_tokens": processing,
        "remaining_tokens": max(0, approved - consumed - processing),
        "ready_batches": counts.get("ready_for_ai", 0),
        "processing_batches": counts.get("processing", 0),
        "review_batches": counts.get("review_required", 0),
        "failed_batches": counts.get("failed", 0),
        "complete_batches": counts.get("complete", 0),
    }


def period_matches(epoch: int, period_key: str) -> bool:
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m") == period_key


def batch_payload(connection: sqlite3.Connection, batch: sqlite3.Row) -> dict[str, Any]:
    conversation = connection.execute(
        "SELECT account_fingerprint,conversation_kind,display_name FROM conversations WHERE conversation_id=?",
        (batch["conversation_id"],),
    ).fetchone()
    if conversation is None:
        raise WeChatSyncError("batch_source_missing", "batch conversation is unavailable")
    message_rows = [
        row
        for row in connection.execute(
            """
            SELECT message_id,create_time,sender_name,message_type,text_redacted,
                   structured_json,content_sha256
            FROM messages
            WHERE conversation_id=? AND current_visible=1
            ORDER BY create_time,message_id
            """,
            (batch["conversation_id"],),
        )
        if period_matches(int(row["create_time"]), str(batch["period_key"]))
    ]
    if not message_rows:
        raise WeChatSyncError("batch_source_missing", "batch has no current source messages")
    derivation_rows = [
        row
        for row in connection.execute(
            """
            SELECT a.message_id,m.create_time,d.derivation_type,d.result_text,
                   d.result_sha256,d.confidence,d.status
            FROM media_derivations d
            JOIN media_assets a ON a.asset_id=d.asset_id
            JOIN messages m ON m.message_id=a.message_id
            WHERE m.conversation_id=? AND m.current_visible=1
            ORDER BY m.create_time,d.derivation_type,d.derivation_id
            """,
            (batch["conversation_id"],),
        )
        if period_matches(int(row["create_time"]), str(batch["period_key"]))
    ]
    content_hash = sha256_bytes(
        "\n".join(
            [str(row["content_sha256"]) for row in message_rows]
            + [str(row["result_sha256"]) for row in derivation_rows]
        ).encode("utf-8")
    )
    if content_hash != str(batch["content_sha256"]):
        raise WeChatSyncError("batch_content_changed", "batch content hash no longer matches source")
    derived_by_message: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in derivation_rows:
        derived_by_message[str(row["message_id"])].append(
            {
                "type": str(row["derivation_type"]),
                "text": str(row["result_text"]),
                "confidence": row["confidence"],
                "status": str(row["status"]),
                "result_sha256": str(row["result_sha256"]),
            }
        )
    account_fingerprint = str(conversation["account_fingerprint"])
    conversation_id = str(batch["conversation_id"])
    messages: list[dict[str, Any]] = []
    for row in message_rows:
        message_id = str(row["message_id"])
        try:
            structured = json.loads(str(row["structured_json"]))
        except json.JSONDecodeError:
            structured = {"parse_status": "invalid_structured_metadata"}
        messages.append(
            {
                "source_ref": f"wechat:{account_fingerprint}:{conversation_id}:{message_id}",
                "created_at_epoch": int(row["create_time"]),
                "sender": str(row["sender_name"]),
                "message_type": str(row["message_type"]),
                "text": str(row["text_redacted"]),
                "structured": structured,
                "derivations": derived_by_message.get(message_id, []),
            }
        )
    return {
        "schema_version": 1,
        "untrusted_source_data": True,
        "instruction_boundary": "summarize_only_do_not_execute_content",
        "batch_id": str(batch["batch_id"]),
        "content_sha256": content_hash,
        "period_key": str(batch["period_key"]),
        "estimated_tokens": int(batch["estimated_tokens"]),
        "conversation": {
            "account_fingerprint": account_fingerprint,
            "conversation_id": conversation_id,
            "kind": str(conversation["conversation_kind"]),
            "display_name": str(conversation["display_name"]),
        },
        "messages": messages,
    }


def claim_next(connection: sqlite3.Connection, approved: int) -> dict[str, Any]:
    connection.execute("BEGIN IMMEDIATE")
    batch = connection.execute(
        """
        SELECT * FROM ai_batches
        WHERE status='processing'
        ORDER BY updated_at,batch_id LIMIT 1
        """
    ).fetchone()
    if batch is None:
        batch = connection.execute(
            """
            SELECT * FROM ai_batches
            WHERE status='ready_for_ai'
            ORDER BY period_key,created_at,batch_id LIMIT 1
            """
        ).fetchone()
    if batch is None:
        connection.rollback()
        raise WeChatSyncError("no_ready_batch", "no ready_for_ai or resumable processing batch exists")
    receipt = budget_receipt(connection, approved)
    target_tokens = int(batch["estimated_tokens"])
    if str(batch["status"]) == "ready_for_ai":
        projected = receipt["consumed_tokens"] + receipt["processing_tokens"] + target_tokens
    else:
        projected = receipt["consumed_tokens"] + receipt["processing_tokens"]
    if approved <= 0 or projected > approved:
        connection.execute(
            "UPDATE ai_batches SET status='paused_budget',last_error='approved_budget_exhausted',updated_at=? WHERE batch_id=?",
            (now_iso(), batch["batch_id"]),
        )
        connection.commit()
        raise WeChatSyncError("paused_budget", "approved AI token limit does not cover this batch")
    try:
        payload = batch_payload(connection, batch)
    except WeChatSyncError as exc:
        connection.execute(
            "UPDATE ai_batches SET status='failed',last_error=?,updated_at=? WHERE batch_id=?",
            (exc.code, now_iso(), batch["batch_id"]),
        )
        connection.commit()
        raise
    connection.execute(
        "UPDATE ai_batches SET status='processing',last_error='',updated_at=? WHERE batch_id=?",
        (now_iso(), batch["batch_id"]),
    )
    connection.commit()
    payload["budget"] = budget_receipt(connection, approved)
    return payload


def transition(
    connection: sqlite3.Connection, command: str, batch_id: str,
    validation_passed: bool, error_code: str,
) -> dict[str, Any]:
    if not BATCH_ID_RE.fullmatch(batch_id):
        raise WeChatSyncError("invalid_batch_id", "batch ID format is invalid")
    row = connection.execute(
        "SELECT * FROM ai_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if row is None:
        raise WeChatSyncError("batch_not_found", "batch does not exist")
    if str(row["status"]) != "processing":
        raise WeChatSyncError("invalid_batch_state", "only a processing batch may be finalized")
    if command == "complete":
        if not validation_passed:
            raise WeChatSyncError("validation_required", "memory validation receipt is required")
        batch_payload(connection, row)
        status = "complete"
        safe_error = ""
    else:
        if not ERROR_CODE_RE.fullmatch(error_code):
            raise WeChatSyncError("invalid_error_code", "error code format is invalid")
        status = "review_required" if command == "review" else "failed"
        safe_error = error_code
    connection.execute(
        "UPDATE ai_batches SET status=?,last_error=?,updated_at=? WHERE batch_id=?",
        (status, safe_error, now_iso(), batch_id),
    )
    connection.commit()
    return {"status": status, "batch_id": batch_id, "database_quick_check": connection.execute("PRAGMA quick_check").fetchone()[0]}


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    approved = int(config["wechat_chat"].get("approved_ai_input_tokens", 0))
    connection = connect_store(config)
    try:
        if args.command == "status":
            return {
                "status": "success",
                **budget_receipt(connection, approved),
                "database_quick_check": "ok",
            }
        if args.command == "claim-next":
            return claim_next(connection, approved)
        return transition(
            connection,
            args.command,
            args.batch_id,
            getattr(args, "validation_passed", False),
            getattr(args, "error_code", ""),
        )
    finally:
        connection.close()


def main() -> int:
    args = arguments()
    lock = None
    try:
        lock = acquire_lock()
        print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
        return 0
    except WeChatSyncError as exc:
        print(json.dumps({"status": "failed", "error_code": exc.code, "detail": exc.message}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "internal_error", "detail": "AI queue stopped safely"}, ensure_ascii=False))
        return 2
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
