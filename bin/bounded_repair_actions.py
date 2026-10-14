"""Fixed repair actions executed by the existing authorized supervisor only."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import os
import subprocess
import uuid
from contextlib import ExitStack
from pathlib import Path
import dingtalk_sync_v2 as syncv2
import business_sync_state as state
import local_sync_repair as local

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "state" / "bounded-repair-actions.lock"
ACTIONS = syncv2.BOUNDED_REPAIR_ACTION_TYPES


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS bounded_repair_requests(
        action_id TEXT PRIMARY KEY, action TEXT NOT NULL, alert_id TEXT NOT NULL,
        run_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, owner_pid INTEGER,
        result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    connection.commit()


def start(config_path, action, alert_id, action_id, requested_run_id):
    for value in (alert_id, action_id, requested_run_id):
        uuid.UUID(value)
    if action not in ACTIONS:
        raise ValueError("repair_action_not_allowlisted")
    if local.maintenance_active():
        return {"status": "blocked", "error_code": "maintenance_active"}
    config = local.memory.load_config(config_path)
    if action == "manual_listener_acceptance":
        import listener_manual_acceptance as listener
        gate = listener.gate(config)
        if not gate.get("available"):
            return {"status": "blocked", "error_code": gate["reason"], "message": gate["message"], "attempt_consumed": False}
    with syncv2.file_lock(LOCK, blocking=False):
        connection = syncv2.connect()
        try:
            initialize(connection)
            old = connection.execute("SELECT * FROM bounded_repair_requests WHERE action_id=?", (action_id,)).fetchone()
            if old:
                if old["action"] != action or old["alert_id"] != alert_id or old["run_id"] != requested_run_id:
                    raise ValueError("repair_action_conflict")
                return {"status": "already_running" if old["state"] in {"queued", "running"} else old["state"], "run_id": old["run_id"]}
            busy = connection.execute("SELECT 1 FROM bounded_repair_requests WHERE state IN ('queued','running')").fetchone()
            if busy:
                return {"status": "blocked", "error_code": "repair_action_busy"}
            run_id = syncv2.start_action_run(action, trigger="manual", requested_run_id=requested_run_id)
            connection.execute("INSERT INTO bounded_repair_requests(action_id,action,alert_id,run_id,state,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?)",
                               (action_id, action, alert_id, run_id, state.now_iso(), state.now_iso()))
            connection.commit()
        finally:
            connection.close()
    return {"status": "started", "queued": True, "run_id": run_id,
            "supervisor_wake_requested": local.wake_supervisor(), "fault_cleared": False}


def execute_queued(config_path):
    if local.maintenance_active():
        return {"status": "maintenance"}
    with ExitStack() as stack:
        try:
            stack.enter_context(syncv2.file_lock(LOCK, blocking=False))
        except syncv2.SyncV2Error as exc:
            if exc.code != "already_running":
                raise
            # Another owner is preparing the queue. Leave its records and
            # attempt budget intact; the next supervisor cycle will retry.
            return {"status": "already_running", "error_code": "repair_action_lock_held",
                    "fault_cleared": False, "attempt_consumed": False}
        connection = syncv2.connect()
        try:
            initialize(connection)
            row = connection.execute("SELECT * FROM bounded_repair_requests WHERE state IN ('queued','running') ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return {"status": "idle"}
            row = dict(row)
            if row["state"] == "running":
                if local.owner_alive(row["owner_pid"]):
                    return {"status": "already_running", "run_id": row["run_id"]}
                result = {"status": "blocked", "error_code": "repair_action_interrupted", "fault_cleared": False}
                connection.execute("UPDATE bounded_repair_requests SET state='blocked',result_json=?,updated_at=? WHERE action_id=?",
                                   (json.dumps(result), state.now_iso(), row["action_id"]))
                connection.commit()
                syncv2.finish_action_run(row["run_id"], "interrupted", error_code=result["error_code"])
                return result
            connection.execute("UPDATE bounded_repair_requests SET state='running',owner_pid=?,updated_at=? WHERE action_id=?",
                               (os.getpid(), state.now_iso(), row["action_id"]))
            connection.commit()
        finally:
            connection.close()
    try:
        config = local.memory.load_config(config_path)
        with local.supervisor_heartbeat(), local.controller.control_lock():
            if local.maintenance_active():
                raise ValueError("maintenance_active")
            if row["action"] == "preserve_current":
                import dingtalk_conflict_recovery as recovery
                local.memory.assert_validation_access(config)
                local.require_index_ready()
                result = recovery.run(config, row["action_id"])
            elif row["action"] == "manual_listener_acceptance":
                import listener_manual_acceptance as listener
                receipt = listener.approve(config, row["action_id"])
                process = subprocess.run(["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/com.example.business-dingtalk-memory-listener"],
                                         capture_output=True, text=True, timeout=8, check=False)
                if process.returncode != 0:
                    # Do not re-dispatch after an uncertain start. The grant and
                    # the old exhausted budget both remain independently visible.
                    listener.finish(config, receipt, "dispatch_uncertain", "event_listener_dispatch_unverified")
                    result = {"status": "blocked", "error_code": "event_listener_dispatch_unverified", "attempt_consumed": False}
                else:
                    result = {"status": "success", "subscription_acceptance": "pending", "automatic_budget_preserved": True}
            else:
                raise ValueError("repair_action_not_allowlisted")
    except Exception as exc:
        result = {"status": "blocked", "error_code": str(getattr(exc, "code", "repair_action_failed")),
                  "message": str(getattr(exc, "message", "本次修复未完成；现版、消息和失败记录已保留。"))}
    result["fault_cleared"] = False
    connection = syncv2.connect()
    try:
        connection.execute("UPDATE bounded_repair_requests SET state=?,result_json=?,updated_at=? WHERE action_id=?",
                           (result["status"], json.dumps(result, ensure_ascii=False), state.now_iso(), row["action_id"]))
        connection.commit()
    finally:
        connection.close()
    syncv2.finish_action_run(row["run_id"], "success" if result["status"] in {"success", "noop"} else "deferred",
                            result=result, error_code=result.get("error_code"))
    return result
