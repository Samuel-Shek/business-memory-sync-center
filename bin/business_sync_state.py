#!/usr/bin/env python3
"""Durable, redacted state for the BUSINESS background sync supervisor.

The tables live beside the existing action ledger.  They contain only source
names, timestamps, counters and bounded error categories; source content,
business identifiers and filenames are never stored here.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import hashlib
import inspect
from contextlib import closing
import ledger_recovery
import os
import re
import sqlite3
import sqlite_attribution
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import dingtalk_sync_v2 as syncv2
import sqlite_read_diagnostics
import sqlite_write_transactions


os.umask(0o077)

DATABASE = syncv2.DATABASE
SOURCE_IDS = ("dingtalk_chat", "dingtalk_minutes", "dingtalk_mail", "win11")
ALERT_SOURCE_IDS = (*SOURCE_IDS, "system", "memory_core")
RUNTIME_FIELDS = frozenset({
    "status", "heartbeat_at", "power_source", "on_ac", "idle_seconds",
    "idle_ready", "current_source", "current_stage", "last_scan_at",
    "next_scan_at", "pending_count", "last_power_change_at", "last_wake_at",
    "blocked_reason", "next_eligible_at", "power_error_code",
    "power_probe_failures", "last_power_probe_at", "last_maintenance_exit_at",
})
SOURCE_FIELDS = frozenset({
    "status", "attempt_count", "probe_count", "last_probe_at", "last_attempt",
    "last_success", "last_failure", "next_due", "blocked_since", "error_code",
    "last_run_id", "recovery_at", "attention_count",
})


def now_dt() -> datetime:
    return datetime.now().astimezone()


def now_iso(value: datetime | None = None) -> str:
    return (value or now_dt()).isoformat(timespec="seconds")


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now_dt().tzinfo)
    return parsed.astimezone()


def _connect(database: Path | None = None, *, readonly: bool = False, timeout: float = 5.0) -> sqlite3.Connection:
    path = Path(database or DATABASE)
    if readonly:
        # Match syncv2.connect_readonly: WAL may need its normal sidecars even
        # for a read. mode=rw prevents creating a missing database; query_only
        # rejects SQL writes before any application query is issued.
        #
        # The lock wait honours the caller's timeout (default 5s) instead of a
        # hardcoded 0.25s. Observed ledger write waits are ~3s at the median and
        # over 20s at the tail, so a quarter second was not "fail fast" — it made
        # every UI read abandon the lock and surface as "temporarily unavailable"
        # while the writer was merely queued and about to succeed.
        uri = path.absolute().as_uri() + "?mode=rw"
        connection = sqlite3.connect(uri, uri=True, timeout=timeout)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=%d" % max(0, int(timeout * 1000)))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        connection = sqlite_attribution.attach(sqlite3.connect(path, timeout=timeout), path)
        try:
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=%d" % max(0, int(timeout * 1000)))
        except BaseException:
            connection.close()
            raise
    connection.row_factory = sqlite3.Row
    return connection


def _write(database: Path | None, phase: str, operation, *, identity=""):
    """Only SQL on the supplied connection may execute inside operation."""
    path = Path(database or DATABASE)
    return ledger_recovery.run(path, phase, lambda: sqlite_write_transactions.write_transaction(path, operation, connector=_connect, phase=phase), identity)


def initialize(database: Path | None = None) -> None:
    return ledger_recovery.run(Path(database or DATABASE), "initialize", lambda: _initialize(database))


def _initialize(database: Path | None = None) -> None:
    path = Path(database or DATABASE)
    signature = hashlib.sha256((inspect.getsource(_initialize) + inspect.getsource(syncv2.initialize)).encode()).hexdigest()
    if path.is_file():
        with closing(_connect(database, readonly=True)) as probe:
            if probe.execute("SELECT 1 FROM sqlite_master WHERE name='ledger_schema_receipts'").fetchone():
                done = probe.execute("SELECT signature FROM ledger_schema_receipts WHERE component='supervisor'").fetchone()
                dirty = probe.execute("SELECT 1 FROM sync_alerts WHERE (status='open' AND severity='error' AND miso_status='not_requested') OR (miso_status='delivered' AND (miso_receipt_hash IS NULL OR length(miso_receipt_hash)!=64 OR miso_receipt_hash GLOB '*[^0-9a-f]*')) LIMIT 1").fetchone()
                if done and done[0] == signature and not dirty:
                    return

    if database is None or Path(database) == syncv2.DATABASE:
        syncv2.initialize()
    connection = _connect(database)
    stamp = now_iso()
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS supervisor_runtime (
              runtime_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              heartbeat_at TEXT,
              power_source TEXT NOT NULL,
              on_ac INTEGER NOT NULL,
              idle_seconds INTEGER,
              idle_ready INTEGER NOT NULL,
              current_source TEXT,
              current_stage TEXT,
              last_scan_at TEXT,
              next_scan_at TEXT,
              pending_count INTEGER NOT NULL DEFAULT 0,
              last_power_change_at TEXT,
              last_wake_at TEXT,
              blocked_reason TEXT,
              next_eligible_at TEXT,
              power_error_code TEXT,
              power_probe_failures INTEGER NOT NULL DEFAULT 0,
              last_power_probe_at TEXT,
              last_maintenance_exit_at TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supervisor_sources (
              source_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              probe_count INTEGER NOT NULL DEFAULT 0,
              last_probe_at TEXT,
              last_attempt TEXT,
              last_success TEXT,
              last_failure TEXT,
              next_due TEXT,
              blocked_since TEXT,
              error_code TEXT,
              last_run_id TEXT,
              recovery_at TEXT,
              attention_count INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_alerts (
              alert_id TEXT PRIMARY KEY,
              dedupe_key TEXT NOT NULL UNIQUE,
              source_id TEXT NOT NULL,
              category TEXT NOT NULL,
              severity TEXT NOT NULL,
              title TEXT NOT NULL,
              detail TEXT NOT NULL,
              status TEXT NOT NULL,
              occurrence_count INTEGER NOT NULL DEFAULT 1,
              attempts INTEGER NOT NULL DEFAULT 0,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              next_reminder TEXT,
              resolved_at TEXT,
              acknowledged_at TEXT,
              macos_status TEXT NOT NULL DEFAULT 'pending',
              macos_requested_at TEXT,
              macos_error_code TEXT,
              macos_attempt_count INTEGER NOT NULL DEFAULT 0,
              feishu_status TEXT NOT NULL DEFAULT 'pending',
              feishu_attempted_at TEXT,
              feishu_receipt_hash TEXT,
              feishu_error_code TEXT,
              miso_status TEXT NOT NULL DEFAULT 'not_requested',
              miso_attempted_at TEXT,
              miso_event_id TEXT,
              miso_receipt_hash TEXT,
              miso_error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alert_delivery_audit (
              delivery_id TEXT PRIMARY KEY,
              alert_id TEXT NOT NULL,
              channel TEXT NOT NULL,
              status TEXT NOT NULL,
              attempted_at TEXT NOT NULL,
              receipt_hash TEXT,
              error_code TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supervisor_incident_receipts (
              incident_id TEXT PRIMARY KEY,
              alert_id TEXT NOT NULL,
              recorded_at TEXT NOT NULL,
              registered_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sync_alerts_status_idx
              ON sync_alerts(status,next_reminder,last_seen);
            CREATE INDEX IF NOT EXISTS sync_alerts_source_idx
              ON sync_alerts(source_id,last_seen);
            CREATE INDEX IF NOT EXISTS alert_delivery_audit_alert_idx
              ON alert_delivery_audit(alert_id,attempted_at);
            """
        )
        runtime_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(supervisor_runtime)")
        }
        if "blocked_reason" not in runtime_columns:
            connection.execute("ALTER TABLE supervisor_runtime ADD COLUMN blocked_reason TEXT")
        if "next_eligible_at" not in runtime_columns:
            connection.execute("ALTER TABLE supervisor_runtime ADD COLUMN next_eligible_at TEXT")
        for definition in (
            "power_error_code TEXT",
            "power_probe_failures INTEGER NOT NULL DEFAULT 0",
            "last_power_probe_at TEXT",
            "last_maintenance_exit_at TEXT",
        ):
            name = definition.split()[0]
            if name not in runtime_columns:
                connection.execute(f"ALTER TABLE supervisor_runtime ADD COLUMN {definition}")
        alert_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(sync_alerts)")
        }
        for definition in (
            "macos_attempt_count INTEGER NOT NULL DEFAULT 0",
            "miso_status TEXT NOT NULL DEFAULT 'not_requested'",
            "miso_attempted_at TEXT",
            "miso_event_id TEXT",
            "miso_receipt_hash TEXT",
            "miso_error_code TEXT",
            "error_code TEXT",
            "notification_revision INTEGER NOT NULL DEFAULT 0",
            "notification_stage TEXT NOT NULL DEFAULT 'initial'",
            "reminder_count INTEGER NOT NULL DEFAULT 0",
            "last_notified_at TEXT",
            "repair_status TEXT NOT NULL DEFAULT 'not_attempted'",
            "repair_attempt_count INTEGER NOT NULL DEFAULT 0",
            "repair_started_at TEXT",
            "repair_finished_at TEXT",
            "repair_error_code TEXT",
            "repair_pid INTEGER",
            "repair_run_id TEXT",
            "macos_visible_at TEXT",
            "legacy_recovery_unverified INTEGER NOT NULL DEFAULT 0",
        ):
            name = definition.split()[0]
            if name not in alert_columns:
                connection.execute(f"ALTER TABLE sync_alerts ADD COLUMN {definition}")
                if name == "legacy_recovery_unverified":
                    connection.execute("UPDATE sync_alerts SET legacy_recovery_unverified=1 WHERE source_id='memory_core' AND category='recovery'")
        audit_columns = {row[1] for row in connection.execute("PRAGMA table_info(alert_delivery_audit)")}
        for definition in ("notification_revision INTEGER NOT NULL DEFAULT 0", "event_id TEXT"):
            if definition.split()[0] not in audit_columns:
                connection.execute(f"ALTER TABLE alert_delivery_audit ADD COLUMN {definition}")
        connection.execute(
            """
            UPDATE sync_alerts
            SET miso_status='pending'
            WHERE status='open' AND severity='error' AND miso_status='not_requested'
            """
        )
        # Old exit-code-only records remain historical evidence, not verified
        # message receipts. Do not convert them to pending or blindly resend.
        connection.execute(
            "UPDATE sync_alerts SET miso_status='unverified',miso_error_code='legacy_receipt_missing' "
            "WHERE miso_status='delivered' AND (miso_receipt_hash IS NULL OR length(miso_receipt_hash)!=64 "
            "OR miso_receipt_hash GLOB '*[^0-9a-f]*')"
        )
        # Keep the persisted/UI deadline consistent with the onset-anchored
        # scheduler without resetting an acknowledgment, revision or receipt.
        for row in connection.execute(
            "SELECT alert_id,first_seen,next_reminder FROM sync_alerts WHERE status='open' "
            "AND source_id IN ('memory_core','dingtalk_chat') AND acknowledged_at IS NULL "
            "AND last_notified_at IS NOT NULL AND reminder_count=0 AND notification_stage='initial'"
        ).fetchall():
            onset, old_due = parse_time(row['first_seen']), parse_time(row['next_reminder'])
            if onset:
                deadline = onset + timedelta(hours=6)
                if old_due is None or old_due > deadline:
                    connection.execute("UPDATE sync_alerts SET next_reminder=? WHERE alert_id=?",
                                       (now_iso(deadline), row['alert_id']))
        connection.execute(
            """
            INSERT OR IGNORE INTO supervisor_runtime(
              runtime_id,status,power_source,on_ac,idle_ready,pending_count,updated_at
            ) VALUES('singleton','starting','unknown',0,0,0,?)
            """,
            (stamp,),
        )
        for source_id in SOURCE_IDS:
            connection.execute(
                """
                INSERT OR IGNORE INTO supervisor_sources(source_id,status,next_due,updated_at)
                VALUES(?,?,?,?)
                """,
                (source_id, "pending", stamp, stamp),
            )
        connection.execute("CREATE TABLE IF NOT EXISTS ledger_schema_receipts(component TEXT PRIMARY KEY,signature TEXT NOT NULL)")
        connection.execute("INSERT OR REPLACE INTO ledger_schema_receipts VALUES('supervisor',?)", (signature,))
        connection.commit()
    finally:
        connection.close()
    try:
        Path(database or DATABASE).chmod(0o600)
    except OSError:
        pass


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("on_ac", "idle_ready"):
        if key in result:
            result[key] = bool(result[key])
    return result


def runtime(database: Path | None = None) -> dict[str, Any]:
    connection = _connect(database, readonly=True)
    try:
        return _row_dict(connection.execute(
            "SELECT * FROM supervisor_runtime WHERE runtime_id='singleton'"
        ).fetchone()) or {}
    finally:
        connection.close()


def update_runtime(database: Path | None = None, **values: Any) -> dict[str, Any]:
    invalid = set(values) - RUNTIME_FIELDS
    if invalid:
        raise ValueError("invalid_runtime_fields")
    stamp = now_iso()
    assignments = [f"{key}=?" for key in values] + ["updated_at=?"]
    normalized = [int(value) if key in {"on_ac", "idle_ready"} else value for key, value in values.items()]
    def transaction(connection):
        connection.execute(
            f"UPDATE supervisor_runtime SET {','.join(assignments)} WHERE runtime_id='singleton'",
            (*normalized, stamp),
        )
    _write(database, "update_runtime", transaction)
    return runtime(database)


def source(source_id: str, database: Path | None = None) -> dict[str, Any]:
    connection = _connect(database, readonly=True)
    try:
        return _row_dict(connection.execute(
            "SELECT * FROM supervisor_sources WHERE source_id=?", (source_id,)
        ).fetchone()) or {}
    finally:
        connection.close()


def sources(database: Path | None = None) -> list[dict[str, Any]]:
    connection = _connect(database, readonly=True)
    try:
        rows = connection.execute("SELECT * FROM supervisor_sources").fetchall()
    finally:
        connection.close()
    by_id = {str(row["source_id"]): _row_dict(row) or {} for row in rows}
    return [by_id[source_id] for source_id in SOURCE_IDS if source_id in by_id]


def update_source(source_id: str, database: Path | None = None, **values: Any) -> dict[str, Any]:
    if source_id not in SOURCE_IDS:
        raise ValueError("source_invalid")
    invalid = set(values) - SOURCE_FIELDS
    if invalid:
        raise ValueError("invalid_source_fields")
    assignments = [f"{key}=?" for key in values] + ["updated_at=?"]
    def transaction(connection):
        connection.execute(
            f"UPDATE supervisor_sources SET {','.join(assignments)} WHERE source_id=?",
            (*values.values(), now_iso(), source_id),
        )
    _write(database, "update_source", transaction, identity=source_id)
    return source(source_id, database)


def mark_all_due(at: datetime | None = None, database: Path | None = None) -> None:
    stamp = now_iso(at)
    def transaction(connection):
        connection.execute(
            "UPDATE supervisor_sources SET next_due=?,updated_at=? WHERE source_id IN (?,?,?,?)",
            (stamp, stamp, *SOURCE_IDS),
        )
    _write(database, "mark_all_due", transaction)


def _safe_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def open_alert(
    source_id: str,
    category: str,
    title: str,
    detail: str,
    *,
    severity: str = "warning",
    attempts: int = 0,
    database: Path | None = None,
    at: datetime | None = None,
    error_code: str | None = None,
    incident_id: str | None = None,
) -> dict[str, Any]:
    if source_id not in ALERT_SOURCE_IDS:
        raise ValueError("source_invalid")
    stamp = now_iso(at)
    dedupe_key = f"{source_id}:{category}"
    def transaction(connection):
        if incident_id is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", incident_id):
                raise ValueError("incident_id_invalid")
            previous = connection.execute(
                "SELECT alert_id FROM supervisor_incident_receipts WHERE incident_id=?", (incident_id,),
            ).fetchone()
            if previous:
                row = connection.execute("SELECT * FROM sync_alerts WHERE alert_id=?", (previous[0],)).fetchone()
                return _row_dict(row) or {"alert_id": previous[0], "historic_incident": True}
        row = connection.execute(
            "SELECT * FROM sync_alerts WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        if row and str(row["status"]) == "open":
            previous_miso = str(row["miso_status"] or "not_requested") if "miso_status" in row.keys() else "not_requested"
            escalation = str(row["severity"] or "warning") != "error" and severity == "error"
            reset_delivery = previous_miso == "not_requested" or escalation
            miso_status = "pending" if previous_miso == "not_requested" or escalation else previous_miso
            connection.execute(
                """
                UPDATE sync_alerts SET occurrence_count=occurrence_count+1,attempts=?,last_seen=?,
                  severity=?,title=?,detail=?,miso_status=?,
                  macos_status=CASE
                    WHEN ? THEN 'pending'
                    WHEN macos_status='failed' AND macos_attempt_count=0 THEN 'retrying'
                    ELSE macos_status END,
                  macos_requested_at=CASE WHEN ? THEN NULL ELSE macos_requested_at END,
                  macos_error_code=CASE WHEN ? THEN NULL ELSE macos_error_code END,
                  macos_attempt_count=CASE WHEN ? THEN 0 ELSE macos_attempt_count END,
                  miso_event_id=CASE WHEN ? THEN NULL ELSE miso_event_id END,
                  miso_receipt_hash=CASE WHEN ? THEN NULL ELSE miso_receipt_hash END,
                  miso_error_code=CASE WHEN ? THEN NULL ELSE miso_error_code END,
                  notification_revision=notification_revision+CASE WHEN ? THEN 1 ELSE 0 END,
                  error_code=COALESCE(?,error_code),updated_at=? WHERE alert_id=?
                """,
                (max(0, int(attempts)), stamp, severity, _safe_text(title, 80),
                 _safe_text(detail, 240), miso_status, escalation, escalation,
                 escalation, escalation, reset_delivery, reset_delivery, reset_delivery,
                 escalation, _safe_text(error_code, 80) or None, stamp, str(row["alert_id"])),
            )
            alert_id = str(row["alert_id"])
        else:
            alert_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO sync_alerts(
                  alert_id,dedupe_key,source_id,category,severity,title,detail,status,
                  attempts,first_seen,last_seen,next_reminder,miso_status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,'open',?,?,?,?,'pending',?,?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                  alert_id=excluded.alert_id,severity=excluded.severity,title=excluded.title,
                  detail=excluded.detail,status='open',occurrence_count=1,attempts=excluded.attempts,
                  first_seen=excluded.first_seen,last_seen=excluded.last_seen,
                  next_reminder=excluded.next_reminder,resolved_at=NULL,acknowledged_at=NULL,
                  macos_status='pending',macos_requested_at=NULL,macos_error_code=NULL,
                  macos_attempt_count=0,miso_status='pending',miso_attempted_at=NULL,
                  miso_event_id=NULL,miso_receipt_hash=NULL,miso_error_code=NULL,
                  notification_revision=0,notification_stage='initial',reminder_count=0,last_notified_at=NULL,
                  repair_status='not_attempted',repair_attempt_count=0,repair_started_at=NULL,
                  repair_finished_at=NULL,repair_error_code=NULL,repair_pid=NULL,repair_run_id=NULL,macos_visible_at=NULL,
                  created_at=excluded.created_at,updated_at=excluded.updated_at
                """,
                (alert_id, dedupe_key, source_id, category, severity, _safe_text(title, 80),
                 _safe_text(detail, 240), max(0, int(attempts)), stamp, stamp, stamp, stamp, stamp),
            )
            connection.execute("UPDATE sync_alerts SET error_code=? WHERE alert_id=?", (_safe_text(error_code, 80) or None, alert_id))
        if incident_id is not None:
            connection.execute("INSERT INTO supervisor_incident_receipts VALUES(?,?,?,?)",
                               (incident_id, alert_id, stamp, now_iso()))
        row = connection.execute("SELECT * FROM sync_alerts WHERE alert_id=?", (alert_id,)).fetchone()
        return _row_dict(row) or {}
    return _write(database, "open_alert", transaction, identity=dedupe_key)


def resolve_alert(
    source_id: str,
    category: str,
    *,
    title: str,
    detail: str,
    database: Path | None = None,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    stamp = now_iso(at)
    # Most health checks have nothing to resolve. Avoid becoming a writer just
    # to discover that; the transaction still rechecks the row under its lock.
    connection = _connect(database, readonly=True)
    try:
        present = connection.execute(
            "SELECT 1 FROM sync_alerts WHERE dedupe_key=? AND status='open'",
            (f"{source_id}:{category}",),
        ).fetchone()
    finally:
        connection.close()
    if not present:
        return None
    def transaction(connection):
        row = connection.execute(
            "SELECT * FROM sync_alerts WHERE dedupe_key=? AND status='open'",
            (f"{source_id}:{category}",),
        ).fetchone()
        if not row:
            return None
        connection.execute(
            """
            UPDATE sync_alerts SET status='resolved',resolved_at=?,next_reminder=NULL,updated_at=?
            WHERE alert_id=?
            """,
            (stamp, stamp, str(row["alert_id"])),
        )
        recovery_id = str(uuid.uuid4())
        recovery_key = f"{source_id}:{category}:recovery:{row['alert_id']}"
        connection.execute(
            """
            INSERT OR IGNORE INTO sync_alerts(
              alert_id,dedupe_key,source_id,category,severity,title,detail,status,
              attempts,first_seen,last_seen,next_reminder,resolved_at,miso_status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'resolved',?,?,?,?,?,'pending',?,?)
            """,
            (recovery_id, recovery_key, source_id, "recovery", "info",
             _safe_text(title, 80), _safe_text(detail, 240), int(row["attempts"] or 0),
             stamp, stamp, stamp, stamp, stamp, stamp),
        )
        recovery = connection.execute(
            "SELECT * FROM sync_alerts WHERE dedupe_key=?", (recovery_key,)
        ).fetchone()
        return _row_dict(recovery)
    return _write(database, "resolve_alert", transaction, identity=f"{source_id}:{category}")


def record_recovery(
    source_id: str,
    reason_key: str,
    *,
    title: str,
    detail: str,
    attempts: int = 0,
    database: Path | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Create one informational recovery row for a failure that had no open alert."""
    stamp = now_iso(at)
    alert_id = str(uuid.uuid4())
    dedupe_key = f"{source_id}:recovery:{_safe_text(reason_key, 80)}"
    def transaction(connection):
        connection.execute(
            """
            INSERT OR IGNORE INTO sync_alerts(
              alert_id,dedupe_key,source_id,category,severity,title,detail,status,
              attempts,first_seen,last_seen,next_reminder,resolved_at,miso_status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'resolved',?,?,?,?,?,'pending',?,?)
            """,
            (alert_id, dedupe_key, source_id, "recovery", "info", _safe_text(title, 80),
             _safe_text(detail, 240), max(0, int(attempts)), stamp, stamp, stamp, stamp, stamp, stamp),
        )
        row = connection.execute(
            "SELECT * FROM sync_alerts WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        return _row_dict(row) or {}
    return _write(database, "record_recovery", transaction, identity=dedupe_key)


def due_alerts(database: Path | None = None, at: datetime | None = None) -> list[dict[str, Any]]:
    current = at or now_dt()
    connection = _connect(database, readonly=True)
    try:
        rows = connection.execute(
            """
            SELECT * FROM sync_alerts
            WHERE legacy_recovery_unverified=0 AND (
               ((status='open' AND acknowledged_at IS NULL OR category='recovery') AND
                (macos_status IN ('pending','retrying','requested') OR miso_status IN ('pending','sending')
                 OR (status='open' AND (next_reminder IS NULL OR next_reminder<=?
                     OR (source_id IN ('memory_core','dingtalk_chat') AND reminder_count=0
                         AND last_notified_at IS NOT NULL AND notification_stage='initial'
                         AND julianday(first_seen)<=julianday(?))))))
               OR (miso_event_id IS NOT NULL AND miso_status IN ('pending','sending')))
            ORDER BY status='open' DESC, first_seen ASC LIMIT 20
            """,
            (now_iso(current), now_iso(current - timedelta(hours=6))),
        ).fetchall()
        result = []
        for raw in rows:
            row = _row_dict(raw) or {}
            if row.get("status") == "open" and row.get("error_code") == "validation_inputs_changed":
                source = connection.execute("SELECT blocked_since FROM supervisor_sources WHERE source_id=?", (row["source_id"],)).fetchone()
                since = parse_time(source[0] if source else None) or parse_time(row["first_seen"])
                if since and current < since + timedelta(hours=2):
                    continue
                unfinished = row.get("miso_status") in {"pending", "sending"} or row.get("macos_status") in {"pending", "retrying", "requested"}
                if row.get("last_notified_at") and not unfinished:
                    continue
            result.append(row)
        return result
    finally:
        connection.close()


def claim_delivery(alert_id: str, database: Path | None = None, at: datetime | None = None) -> dict[str, Any]:
    current = at or now_dt()
    def transaction(connection):
        row = _row_dict(connection.execute("SELECT * FROM sync_alerts WHERE alert_id=?", (alert_id,)).fetchone())
        if not row:
            return {}
        previous_event = None
        if row.get("acknowledged_at") and row.get("category") != "recovery":
            return row
        first_request = not row.get("last_notified_at")
        due = parse_time(row.get("next_reminder"))
        onset_anchored = row.get("source_id") in {"memory_core", "dingtalk_chat"}
        first_deadline = (parse_time(row.get("first_seen")) or current) + timedelta(hours=6)
        if onset_anchored and not first_request and int(row.get("reminder_count") or 0) == 0:
            due = min(due, first_deadline) if due else first_deadline
        validation_wait = row.get("error_code") == "validation_inputs_changed"
        is_reminder = not validation_wait and not first_request and row["status"] == "open" and (due is None or current >= due)
        already_overdue = bool(not validation_wait and first_request and onset_anchored and row["status"] == "open" and current >= first_deadline)
        if already_overdue:
            # One immediate escalation, not an initial message followed by a
            # duplicate escalation on the next tick after a long outage.
            connection.execute("UPDATE sync_alerts SET notification_stage='escalation',reminder_count=1 WHERE alert_id=?", (alert_id,))
        if is_reminder:
            previous_event = row.get("miso_event_id")
            count = int(row.get("reminder_count") or 0) + 1
            connection.execute(
                "UPDATE sync_alerts SET notification_revision=notification_revision+1,notification_stage=?,reminder_count=?,"
                "macos_status='pending',macos_attempt_count=0,macos_error_code=NULL,miso_status='pending',"
                "miso_event_id=NULL,miso_receipt_hash=NULL,miso_error_code=NULL WHERE alert_id=?",
                ("escalation" if count == 1 else "daily", count, alert_id),
            )
        if first_request or is_reminder:
            deadline = (first_deadline if onset_anchored else current + timedelta(hours=6)) if first_request and not already_overdue else current + timedelta(hours=24)
            next_due = now_iso(deadline) if row["status"] == "open" else None
            connection.execute("UPDATE sync_alerts SET next_reminder=?,last_notified_at=?,updated_at=? WHERE alert_id=?",
                               (next_due, now_iso(current), now_iso(current), alert_id))
        result = _row_dict(connection.execute("SELECT * FROM sync_alerts WHERE alert_id=?", (alert_id,)).fetchone()) or {}
        if previous_event:
            result["previous_miso_event_id"] = previous_event
        return result
    return _write(database, "claim_delivery", transaction, identity=alert_id)


def mark_delivery(alert_id: str, channel: str, status: str, *, event_id=None,
                  receipt_hash=None, error_code=None, database=None, at=None, count_attempt=True, notification_revision=None) -> None:
    path = Path(database or DATABASE)
    if notification_revision is None:
        with closing(_connect(database, readonly=True)) as c:
            row = c.execute("SELECT notification_revision FROM sync_alerts WHERE alert_id=?", (alert_id,)).fetchone()
        if row is None:return
        notification_revision=row[0]
    if channel not in {"macos", "miso", "feishu"}:
        raise ValueError("channel_invalid")
    if channel == "miso" and status == "delivered" and not re.fullmatch(r"[0-9a-f]{64}", str(receipt_hash or "")):
        status, error_code = "unverified", "message_receipt_missing"
    payload = dict(alert_id=alert_id, channel=channel, status=_safe_text(status,32),
                   event_id=_safe_text(event_id,80) or None, receipt_hash=_safe_text(receipt_hash,80) or None,
                   error_code=_safe_text(error_code,80) or None, count_attempt=bool(count_attempt),
                   revision=notification_revision)
    token = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    payload['stamp'] = now_iso(at)
    pending = ledger_recovery.spool(path, token, payload)
    saved = ledger_recovery.read(pending)
    if saved.get('status') == 'applied': return
    _apply_delivery_payload(saved['payload'], path, token)
    ledger_recovery.finish_spool(pending)


def _apply_delivery_payload(p, database, token):
    def transaction(c):
        row = c.execute("SELECT notification_revision FROM sync_alerts WHERE alert_id=?", (p['alert_id'],)).fetchone()
        if row is None or row[0] != p['revision']: return
        inserted = c.execute("INSERT OR IGNORE INTO alert_delivery_audit(delivery_id,alert_id,channel,status,attempted_at,receipt_hash,error_code,created_at,notification_revision,event_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (token,p['alert_id'],p['channel'],p['status'],p['stamp'],p['receipt_hash'],p['error_code'],p['stamp'],p['revision'],p['event_id'])).rowcount
        if not inserted: return
        channel=p['channel']; status=p['status']; stamp=p['stamp']
        if channel=='macos':
            fields="macos_status=?,macos_requested_at=?,macos_error_code=?,macos_attempt_count=macos_attempt_count+?,macos_visible_at=?"
            values=(status,stamp,p['error_code'],int(p['count_attempt']),stamp if status=='visible' else None)
        elif channel=='feishu':
            fields="feishu_status=?,feishu_attempted_at=?,feishu_receipt_hash=?,feishu_error_code=?"
            values=(status,stamp,p['receipt_hash'],p['error_code'])
        else:
            fields="miso_status=?,miso_attempted_at=?,miso_event_id=COALESCE(?,miso_event_id),miso_receipt_hash=?,miso_error_code=?"
            values=(status,stamp,p['event_id'],p['receipt_hash'],p['error_code'])
        c.execute(f"UPDATE sync_alerts SET {fields},updated_at=? WHERE alert_id=?",(*values,stamp,p['alert_id']))
    return _write(database, 'mark_delivery', transaction, identity=token)


def acknowledge(alert_id: str, database: Path | None = None) -> dict[str, Any] | None:
    def transaction(connection):
        stamp = now_iso()
        connection.execute(
            "UPDATE sync_alerts SET acknowledged_at=?,next_reminder=NULL,updated_at=? WHERE alert_id=?",
            (stamp, stamp, alert_id),
        )
        return _row_dict(connection.execute(
            "SELECT * FROM sync_alerts WHERE alert_id=?", (alert_id,)
        ).fetchone())
    return _write(database, "acknowledge", transaction, identity=alert_id)


def alerts(
    database: Path | None = None,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    bounded = max(1, min(200, int(limit)))
    arguments: list[Any] = []
    clause = ""
    if status in {"open", "resolved"}:
        clause = "WHERE status=?"
        arguments.append(status)
    rows = sqlite_read_diagnostics.read_rows(
        Path(database or DATABASE), f"SELECT * FROM sync_alerts {clause} ORDER BY last_seen DESC LIMIT ?",
        (*arguments, bounded), connector=_connect,
    )
    return [_row_dict(row) or {} for row in rows]


def open_alert_count(database: Path | None = None) -> int:
    connection = _connect(database, readonly=True)
    try:
        row = connection.execute("SELECT COUNT(*) FROM sync_alerts WHERE status='open'").fetchone()
        return int(row[0] or 0)
    finally:
        connection.close()


def earliest_due(rows: Iterable[dict[str, Any]]) -> str | None:
    values = [parse_time(row.get("next_due")) for row in rows]
    valid = [value for value in values if value is not None]
    return now_iso(min(valid)) if valid else None


def effective_alert_state(
    row: dict[str, Any],
    *,
    maintenance_active: bool = False,
    post_maintenance_pending: bool = False,
) -> str:
    if str(row.get("status") or "resolved") != "open":
        if row.get("legacy_recovery_unverified"):
            return "unknown"
        return "recovered" if str(row.get("category") or "") == "recovery" else "resolved"
    if (
        (maintenance_active or post_maintenance_pending)
        and str(row.get("source_id") or "") == "memory_core"
        and str(row.get("category") or "") == "core_failure"
    ):
        return "maintenance_pending"
    return "critical" if str(row.get("severity") or "warning") == "error" else "warning"


def delivery_summary(row: dict[str, Any]) -> dict[str, Any]:
    macos_status = str(row.get("macos_status") or "not_requested")
    miso_status = str(row.get("miso_status") or "not_requested")
    problem_states = {"failed", "permission_off", "blocked", "uncertain", "superseded"}
    pending_states = {"pending", "retrying", "sending"}
    if macos_status in problem_states or miso_status in problem_states:
        summary = "failed"
    elif macos_status in pending_states or miso_status in pending_states:
        summary = "pending"
    elif macos_status == "not_requested" and miso_status == "not_requested":
        summary = "not_requested"
    elif miso_status == "delivered" and re.fullmatch(r"[0-9a-f]{64}", str(row.get("miso_receipt_hash") or "")) and macos_status == "visible":
        summary = "complete"
    else:
        summary = "unconfirmed"
    return {
        "summary": summary,
        "application": {"status": "visible"},
        "macos": {
            "status": macos_status,
            "attempted_at": row.get("macos_requested_at"),
            "attempt_count": int(row.get("macos_attempt_count") or 0),
            "error_code": row.get("macos_error_code"),
            "visible_at": row.get("macos_visible_at"),
            "read_confirmed": False,
        },
        "miso": {
            "status": miso_status,
            "attempted_at": row.get("miso_attempted_at"),
            "receipt_confirmed": bool(re.fullmatch(r"[0-9a-f]{64}", str(row.get("miso_receipt_hash") or ""))),
            "read_confirmed": False,
            "error_code": row.get("miso_error_code"),
        },
        "legacy_feishu": {
            "status": str(row.get("feishu_status") or "not_requested"),
            "attempted_at": row.get("feishu_attempted_at"),
            "receipt_hash": row.get("feishu_receipt_hash"),
            "error_code": row.get("feishu_error_code"),
        },
    }


def public_alert(
    row: dict[str, Any],
    *,
    maintenance_active: bool = False,
    post_maintenance_pending: bool = False,
    source_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the exact safe subset exposed by the loopback API."""
    payload = {
        key: row.get(key)
        for key in (
            "alert_id", "source_id", "category", "severity", "title", "detail",
            "status", "occurrence_count", "attempts", "first_seen", "last_seen",
            "resolved_at", "acknowledged_at", "macos_status", "macos_requested_at",
            "macos_error_code", "feishu_status", "feishu_attempted_at",
            "feishu_receipt_hash", "feishu_error_code", "miso_status",
            "miso_attempted_at", "miso_error_code", "macos_attempt_count",
            "next_reminder", "notification_revision", "notification_stage", "reminder_count",
            "repair_status", "repair_attempt_count", "repair_started_at", "repair_finished_at",
            "repair_error_code", "legacy_recovery_unverified", "error_code",
            "repair_run_id",
        )
    }
    # Compatibility: occurrence_count remains available for one release, but
    # its meaning is repeated confirmation of the same deduplicated incident,
    # not a count of distinct failures.
    payload["confirmation_count"] = int(row.get("occurrence_count") or 0)
    payload["last_confirmed"] = row.get("last_seen")
    payload["effective_state"] = effective_alert_state(
        row,
        maintenance_active=maintenance_active,
        post_maintenance_pending=post_maintenance_pending,
    )
    payload["delivery"] = delivery_summary(row)
    source_error = str((source_state or {}).get("error_code") or "")
    if (
        payload["status"] == "open"
        and payload["source_id"] == "dingtalk_mail"
        and payload["category"] == "sync_failure"
        and source_error in {
            "read_safety_unconfirmed", "read_safety_failed",
            "read_safety_server_unverified", "read_safety_receipt_unsafe",
        }
    ):
        payload["action_required"] = "email_read_safety_confirmation"
        payload["action_label"] = "需要一次人工安全确认"
    else:
        payload["action_required"] = None
        payload["action_label"] = None
    return payload
