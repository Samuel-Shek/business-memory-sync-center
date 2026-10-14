#!/usr/bin/env python3
"""Resume only validation, with positive same-source stage and receipt evidence."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import automatic_memory_validation as validation
import business_sync_state as state
import validation_evidence as evidence

CHANGE = "validation_inputs_changed"
RECOVERABLE = frozenset({CHANGE, "validation_failed"})
WAIT_TITLE = "聊天已同步，等待资料稳定"


def pending(row: dict) -> bool:
    return row.get("error_code") == CHANGE or (row.get("source_id") == "dingtalk_chat" and row.get("error_code") == "validation_failed")


def request_validation(root: Path, point: dict) -> None:
    """Coalesce requests without invalidating the business snapshot or budgets."""
    path = root / "state/validation-request.json"
    previous = validation.read_object(path)
    requested = state.parse_time(point.get("pending_since"))
    before = state.parse_time(previous.get("requested_at"))
    if requested and (before is None or requested > before):
        validation.atomic_json(path, {"schema_version": 1, "requested_at": state.now_iso(requested),
                                    "source_run_id": point["source_run_id"],
                                    "required_generation": point.get("required_generation")})


def checkpoint(run_id: str, source_id: str, database: Path | None = None) -> dict | None:
    """Old receipts may lack a generation, but never lack completed source steps."""
    with state._connect(database, readonly=True) as connection:
        run = connection.execute("SELECT * FROM action_runs WHERE run_id=?", (run_id,)).fetchone()
        steps = [dict(row) for row in connection.execute("SELECT * FROM action_steps WHERE run_id=? ORDER BY step_order", (run_id,))]
    if not run or run["status"] not in {"failed", "deferred"} or run["error_code"] not in RECOVERABLE:
        return None
    run = dict(run)
    if source_id != "dingtalk_chat" and run["error_code"] != CHANGE:
        return None
    expected_type = {source_id, "sync_now", "all"}
    if run["action_type"] not in expected_type:
        return None
    by_key = {row["step_key"]: row for row in steps}
    required = {"prepare", "wait_review", "decision_registration"}
    required |= {"chat_reconcile", "chat_memory"} if source_id == "dingtalk_chat" else {"recording_scan", "recording_fetch", "recording_source", "recording_memory"}
    last = by_key.get("dingtalk_validation", {})
    if not required <= by_key.keys() or last.get("error_code") != run["error_code"] or last.get("status") not in {"failed", "deferred"}:
        return None
    preceding = [row for row in steps if row["step_order"] < last["step_order"]]
    if not required <= {row["step_key"] for row in preceding} or any(row["step_order"] > last["step_order"] for row in steps):
        return None
    if any(row["status"] not in {"success", "noop"} for row in preceding):
        return None
    try:
        detail = json.loads(last.get("safe_detail_json") or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(detail, dict) or (detail.get("required_generation") is not None and (type(detail["required_generation"]) is not int or detail["required_generation"] < 0)):
        return None
    return {"source_id": source_id, "source_run_id": run_id,
            "failure_code": run["error_code"], "source_started_at": run["started_at"],
            "pending_since": last.get("completed_at") or run["completed_at"],
            "required_generation": detail.get("required_generation"),
            "completed_steps": [row["step_key"] for row in preceding],
            "legacy_checkpoint": detail.get("required_generation") is None}


def reconcile(service, config: dict, current: datetime) -> None:
    root = service.config_path.parent
    export_committed_receipts(root, service.database)
    if not any(pending(row) for row in state.sources(service.database)):
        return
    # Same lock order as a source controller: never close against a concurrent
    # source run or a new committed generation. Probes never wait holding locks.
    with validation.try_lock(root / "state/sync-center-control.lock") as control:
        if control is None:
            return
        with validation.try_lock(root / "state/memory-index.lock") as memory:
            if memory is None:
                return
            _reconcile(service, config, current)


def export_committed_receipts(root, database):
    # The transaction is authoritative. Repair an interrupted file export
    # without repeating source work or changing historical action results.
    with state._connect(database, readonly=True) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='validation_recovery_receipts'").fetchone():
            return
        receipts = connection.execute("SELECT source_run_id,receipt_json FROM validation_recovery_receipts").fetchall()
    for run_id, encoded in receipts:
        uuid.UUID(run_id)
        receipt = json.loads(encoded)
        destination = root/'state/validation-recovery'/(run_id+'.json')
        if validation.read_object(destination) != receipt:
            validation.atomic_json(destination, receipt)


def commit_recovery(database, row, point, alerts, receipt_at, current, closure):
    stamp = state.now_iso(current)
    with state._connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        latest = connection.execute("SELECT * FROM supervisor_sources WHERE source_id=?", (row["source_id"],)).fetchone()
        if not latest or latest["last_run_id"] != row["last_run_id"] or latest["error_code"] != row["error_code"] or latest["last_failure"] != row.get("last_failure") or latest["attempt_count"] != row.get("attempt_count"):
            return False
        if connection.execute("SELECT 1 FROM action_runs WHERE action_type IN (?, 'sync_now', 'all') AND started_at>=? AND run_id!=? LIMIT 1",
                              (row["source_id"], point["source_started_at"], row["last_run_id"])).fetchone():
            return False
        for alert in alerts:
            fresh = connection.execute("SELECT * FROM sync_alerts WHERE alert_id=?", (alert["alert_id"],)).fetchone()
            if not fresh or fresh["status"] != "open" or fresh["error_code"] != row["error_code"] or fresh["attempts"] != alert["attempts"]:
                return False
        connection.execute("CREATE TABLE IF NOT EXISTS validation_recovery_receipts(source_run_id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO validation_recovery_receipts VALUES(?,?)",
                           (point["source_run_id"], json.dumps({**closure, "state": "resolved", "resolved_at": stamp}, ensure_ascii=False, sort_keys=True)))
        for alert in alerts:
            connection.execute("UPDATE sync_alerts SET status='resolved',resolved_at=?,next_reminder=NULL,updated_at=? WHERE alert_id=?",
                               (stamp, stamp, alert["alert_id"]))
            key = f"{row['source_id']}:{alert['category']}:recovery:{alert['alert_id']}"
            connection.execute("""INSERT OR IGNORE INTO sync_alerts(
                alert_id,dedupe_key,source_id,category,severity,title,detail,status,attempts,
                first_seen,last_seen,next_reminder,resolved_at,miso_status,created_at,updated_at)
                VALUES(?,?,?,'recovery','info',?,?,'resolved',?,?,?,?,?,'pending',?,?)""",
                (str(uuid.uuid4()), key, row["source_id"], "聊天记忆校验已恢复" if row["source_id"] == "dingtalk_chat" else "听记记忆校验已恢复",
                 "原同步步骤已完成；后续完整校验回执匹配当前资料，历史记录保留。", alert["attempts"], stamp, stamp, stamp, stamp, stamp, stamp))
        connection.execute("""UPDATE supervisor_sources SET status='healthy',error_code=NULL,attempt_count=0,
            probe_count=0,blocked_since=NULL,last_success=?,recovery_at=?,next_due=?,updated_at=? WHERE source_id=?""",
            (state.now_iso(receipt_at), stamp, state.now_iso(current + timedelta(minutes=5)), stamp, row["source_id"]))
    return True


def _reconcile(service, config: dict, current: datetime) -> None:
    root = service.config_path.parent
    rows = [row for row in state.sources(service.database) if pending(row) and row["source_id"] in {"dingtalk_chat", "dingtalk_minutes"}]
    if not rows:
        return
    worker = validation.read_object(root / "state/automatic-memory-validation.json")
    success = validation.read_object(root / "state/last-memory-validation-success.json")
    failure = validation.read_object(root / "state/last-memory-validation-failure.json")
    index = validation.index_state(root)
    receipt_at = state.parse_time(success.get("recorded_at"))
    failed_at = state.parse_time(failure.get("recorded_at"))
    matches = bool(index["ready"] and receipt_at and (not failure or failed_at and receipt_at > failed_at)
                   and evidence.receipt_is_current(root, success, index["dirty"], index["applied"]))
    for row in rows:
        source = row["source_id"]
        point = checkpoint(str(row.get("last_run_id") or ""), source, service.database)
        failure_times = [state.parse_time(value) for value in
                         ((point or {}).get("pending_since"), row.get("last_failure"), row.get("blocked_since"))]
        since = max((value for value in failure_times if value is not None), default=current)
        with state._connect(service.database, readonly=True) as connection:
            newer = connection.execute("SELECT 1 FROM action_runs WHERE action_type IN (?, 'sync_now', 'all') AND started_at>? AND run_id!=? LIMIT 1",
                                       (source, (point or {}).get("source_started_at") or state.now_iso(since), row.get("last_run_id") or "")).fetchone()
        # A newer source run owns its own result; no older checkpoint may turn it green.
        if newer:
            continue
        alerts = [a for a in state.alerts(service.database, status="open")
                  if a["source_id"] == source and a.get("error_code") == row.get("error_code") and a.get("category") in {"sync_failure", "sync_block"}]
        required = (point or {}).get("required_generation")
        can_close = bool(point and matches and receipt_at > since
                         and (required is None or int(index["applied"]) >= int(required))
                         and all(state.parse_time(a["first_seen"]) is not None and receipt_at > state.parse_time(a["first_seen"]) for a in alerts))
        latest = state.source(source, service.database)
        if latest.get("last_run_id") != row.get("last_run_id") or latest.get("error_code") != row.get("error_code"):
            continue
        if can_close:
            closure = {**point, "validation_run_id": success.get("run_id"),
                       "validation_receipt_at": success["recorded_at"],
                       "validation_context": success["validation_context"],
                       "validated_generation": index["applied"],
                       "prior_source_attempt_count": row.get("attempt_count"),
                       "alert_ids": [a["alert_id"] for a in alerts], "state": "validated_pending_closure"}
            destination = root / "state/validation-recovery" / (point["source_run_id"] + ".json")
            validation.atomic_json(destination, closure)
            if not commit_recovery(service.database, row, point, alerts, receipt_at, current, closure):
                continue
            validation.atomic_json(destination, {**closure, "state": "resolved", "resolved_at": state.now_iso(current)})
            continue
        if row.get("error_code") != CHANGE:
            # A strict failure stays failed until positive, current evidence exists.
            continue
        if point:
            request_validation(root, point)
        next_at = worker.get("next_retry_at") or state.now_iso(current + timedelta(minutes=1))
        state.update_source(source, service.database, status="deferred", error_code=CHANGE,
                            blocked_since=state.now_iso(since), next_due=next_at)
        # Reclassify the old false failure without deleting its history or
        # resolving it prematurely. Workbench snapshots retain original wording.
        title = (WAIT_TITLE if source == "dingtalk_chat" else "听记已同步，等待资料稳定") if point else "校验前置步骤等待核实"
        detail = "原同步步骤已完成；等待稳定后只重验记忆。" if point else "原同步检查点暂不能完整核实；保留记录，等待校验证据。"
        with state._connect(service.database) as connection:
            for alert in alerts:
                connection.execute("UPDATE sync_alerts SET title=?,detail=?,severity='warning' WHERE alert_id=? AND error_code=?",
                                   (title, detail, alert["alert_id"], CHANGE))
        if current - since >= timedelta(hours=2) and not alerts:
            state.open_alert(source, "sync_block", "记忆校验等待已超过 2 小时", detail,
                             severity="warning", error_code=CHANGE, database=service.database, at=current)


def wait_view(root: Path, row: dict, current: datetime, *, database: Path | None = None) -> dict:
    import sqlite3
    try:
        completed = checkpoint(str(row.get("last_run_id") or ""), str(row.get("source_id") or ""), database) is not None
    except (OSError, sqlite3.Error, ValueError):
        completed = False
    worker = validation.read_object(root / "state/automatic-memory-validation.json")
    since = state.parse_time(row.get("blocked_since")) or state.parse_time(row.get("last_failure")) or current
    return {"sync_completed": completed, "phase": "validating" if worker.get("status") == "running" or worker.get("phase") == "inventory" and worker.get("status") == "waiting_stable" else "waiting_stable",
            "pending_since": state.now_iso(since), "wait_seconds": max(0, int((current - since).total_seconds())),
            "next_retry_at": worker.get("next_retry_at") or row.get("next_due"),
            "defer_count": worker.get("defer_count", 0), "timeout_count": worker.get("timeout_count", 0)}
