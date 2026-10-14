#!/usr/bin/env python3
"""Produce a privacy-safe seven-day WeChat pilot estimate from local metadata."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import os
import sqlite3
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from wechat_common import atomic_write, now_iso


os.umask(0o077)
ROOT = Path(__file__).resolve().parents[1]


def directory_bytes(root: Path) -> int:
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
    return total


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    return parser.parse_args()


def document_units(connection: sqlite3.Connection, run_id: str, cutoff: int) -> dict[str, int]:
    totals = {"pdf_pages": 0, "powerpoint_slides": 0, "excel_sheets": 0, "word_paragraphs": 0}
    for row in connection.execute(
        """
        SELECT d.result_text
        FROM media_derivations d
        JOIN media_assets a ON a.asset_id=d.asset_id
        JOIN messages m ON m.message_id=a.message_id
        JOIN conversation_scans s ON s.conversation_id=m.conversation_id
        WHERE d.derivation_type='document_text' AND s.run_id=? AND m.create_time>=?
        """,
        (run_id, cutoff),
    ):
        first = str(row[0]).splitlines()[0] if row[0] else ""
        try:
            metadata = json.loads(first)
        except json.JSONDecodeError:
            continue
        totals["pdf_pages"] += int(metadata.get("pages", 0) or 0)
        totals["powerpoint_slides"] += int(metadata.get("slides", 0) or 0)
        totals["excel_sheets"] += int(metadata.get("sheets", 0) or 0)
        totals["word_paragraphs"] += int(metadata.get("paragraphs", 0) or 0)
    return totals


def main() -> int:
    args = arguments()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    chat: dict[str, Any] = config["wechat_chat"]
    source_root = Path(config["external_root"]) / "wechat-chat"
    database = source_root / "state" / "chat.sqlite"
    if not database.is_file():
        print(json.dumps({"status": "failed", "error_code": "source_store_missing"}))
        return 2
    # A WAL database may need to recreate its shared-memory sidecar after the
    # last writer exits.  Opening the main file with mode=ro prevents that on
    # some macOS SQLite builds and makes an otherwise read-only report fail.
    # Keep the SQL connection query-only while allowing SQLite to maintain the
    # WAL coordination sidecar in the owner-only state directory.
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            print(json.dumps({"status": "failed", "error_code": "database_integrity_failed"}))
            return 2
        pilot_run = connection.execute(
            """
            SELECT run_id,cutoff_epoch,messages_seen,status,finished_at
            FROM sync_runs
            WHERE mode='pilot' AND finished_at IS NOT NULL
              AND status IN ('paused_budget','media_pending','ready_for_ai','success')
            ORDER BY finished_at DESC LIMIT 1
            """
        ).fetchone()
        if pilot_run is None or int(pilot_run["messages_seen"]) <= 0:
            print(json.dumps({"status": "failed", "error_code": "pilot_not_complete"}))
            return 2
        run_id = str(pilot_run["run_id"])
        days = int(chat.get("pilot_days", 7))
        cutoff = int(pilot_run["cutoff_epoch"])
        # Old stores may predate persistence of the active pilot cutoff. Keep the
        # report bounded to the requested window in that case, while preserving
        # the collector's exact cutoff for current stores.
        requested_cutoff = int((datetime.now().astimezone() - timedelta(days=days)).timestamp())
        if cutoff < requested_cutoff - 3600:
            cutoff = requested_cutoff
        messages = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(LENGTH(text_redacted)),0),MIN(create_time),MAX(create_time) "
            "FROM messages m JOIN conversation_scans s ON s.conversation_id=m.conversation_id "
            "WHERE s.run_id=? AND s.status='complete' AND m.current_visible=1 AND m.create_time>=?",
            (run_id, cutoff),
        ).fetchone()
        if int(messages[0]) <= 0:
            print(json.dumps({"status": "failed", "error_code": "pilot_not_complete"}))
            return 2
        scopes = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT c.conversation_kind,COUNT(*)
                FROM conversation_scans s
                JOIN conversations c ON c.conversation_id=s.conversation_id
                WHERE s.run_id=? AND s.status='complete'
                GROUP BY c.conversation_kind
                """,
                (run_id,),
            )
        }
        if scopes.get("direct", 0) != 1 or scopes.get("group", 0) != 1:
            print(json.dumps({"status": "failed", "error_code": "pilot_scope_incomplete"}))
            return 2
        media = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT a.media_type,COUNT(*)
                FROM media_assets a
                JOIN messages m ON m.message_id=a.message_id
                JOIN conversation_scans s ON s.conversation_id=m.conversation_id
                WHERE s.run_id=? AND m.create_time>=?
                GROUP BY a.media_type
                """,
                (run_id, cutoff),
            )
        }
        media_states = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT CASE WHEN a.error_code IN ('video_waiting_instruction','media_type_waiting_confirmation') THEN 'waiting_instruction' ELSE a.status END,COUNT(*)
                FROM media_assets a
                JOIN messages m ON m.message_id=a.message_id
                JOIN conversation_scans s ON s.conversation_id=m.conversation_id
                WHERE s.run_id=? AND m.create_time>=?
                GROUP BY CASE WHEN a.error_code IN ('video_waiting_instruction','media_type_waiting_confirmation') THEN 'waiting_instruction' ELSE a.status END
                """,
                (run_id, cutoff),
            )
        }
        if media_states.get("queued", 0) or media_states.get("resolved", 0):
            print(json.dumps({"status": "failed", "error_code": "pilot_media_pending"}))
            return 2
        durations = connection.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN a.media_type='voice' THEN a.duration_seconds ELSE 0 END),0),
              COALESCE(SUM(CASE WHEN a.media_type='video' THEN a.duration_seconds ELSE 0 END),0)
            FROM media_assets a
            JOIN messages m ON m.message_id=a.message_id
            JOIN conversation_scans s ON s.conversation_id=m.conversation_id
            WHERE s.run_id=? AND m.create_time>=?
            """,
            (run_id, cutoff),
        ).fetchone()
        estimated_tokens = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(b.estimated_tokens),0)
                FROM ai_batches b
                JOIN conversation_scans s ON s.conversation_id=b.conversation_id
                WHERE s.run_id=? AND b.status IN ('paused_budget','ready_for_ai','processing','complete')
                """,
                (run_id,),
            ).fetchone()[0]
        )
        redactions = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(m.redaction_count),0)
                FROM messages m
                JOIN conversation_scans s ON s.conversation_id=m.conversation_id
                WHERE s.run_id=? AND m.create_time>=?
                """,
                (run_id, cutoff),
            ).fetchone()[0]
        )
        current_bytes = directory_bytes(source_root)
        annual_bytes = int(current_bytes * 365 / max(1, days))
        unit_price = chat.get("ai_input_usd_per_million")
        estimated_cost = round(estimated_tokens * float(unit_price) / 1_000_000, 4) if unit_price is not None else None
        receipt = {
            "schema_version": 1,
            "generated_at": now_iso(),
            "status": "success",
            "pilot_days": days,
            "scope_counts": {"direct": scopes.get("direct", 0), "group": scopes.get("group", 0)},
            "message_count": int(messages[0]),
            "character_count": int(messages[1]),
            "message_time_min_epoch": messages[2],
            "message_time_max_epoch": messages[3],
            "redaction_count": redactions,
            "media_counts": media,
            "media_status_counts": media_states,
            "voice_minutes": round(float(durations[0]) / 60, 2),
            "video_minutes": round(float(durations[1]) / 60, 2),
            "document_units": document_units(connection, run_id, cutoff),
            "current_storage_bytes": current_bytes,
            "estimated_annual_storage_bytes": annual_bytes,
            "estimated_codex_input_tokens": estimated_tokens,
            "estimated_input_cost_usd": estimated_cost,
            "pricing_status": "configured_current_price" if unit_price is not None else "current_price_review_required",
            "approval_status": "paused_budget" if int(chat.get("approved_ai_input_tokens", 0)) <= 0 else "approved_limit_configured",
            "coverage_claim": "local_records_only_not_twelve_month_completeness",
        }
    finally:
        connection.close()
    atomic_write(source_root / "state" / "pilot_estimate.json", json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
