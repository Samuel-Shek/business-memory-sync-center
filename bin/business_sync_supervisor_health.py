#!/usr/bin/env python3
"""Independent, content-free health sentinel for the BUSINESS sync supervisor."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import json
import os
import re
import subprocess
import sqlite3
import ledger_recovery
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

import business_sync_state as state
import supervisor_incidents as incidents
import supervisor_liveness as liveness
import supervisor_health_recovery as recovery


def deliver_due_alerts(config, database):
    # Do not import/start supervisor scheduling just to inspect owner health.
    from business_sync_supervisor import deliver_due_alerts as deliver
    return deliver(config, database)


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
SENTINEL_LOCK = ROOT / "state" / "sync-supervisor-health.lock"
MAINTENANCE_PATH = ROOT / "state" / "maintenance-mode.json"
SUPERVISOR_LABEL = "com.example.business-sync-supervisor"
HEARTBEAT_GRACE = timedelta(minutes=7)


def now_dt() -> datetime:
    return datetime.now().astimezone()


def maintenance_active(path: Path = MAINTENANCE_PATH) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return not isinstance(payload, dict) or str(payload.get("status") or "unknown") != "inactive"


@contextmanager
def sentinel_lock(path: Path = SENTINEL_LOCK) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def launchd_status(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    domain = f"gui/{os.getuid()}/{SUPERVISOR_LABEL}"
    try:
        result = runner(
            ["/bin/launchctl", "print", domain],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"loaded": None, "state": "unknown", "last_exit_code": None}
    text = f"{result.stdout}\n{result.stderr}"
    state_match = re.search(r"^\s*state\s*=\s*([^\n]+)", text, re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code\s*=\s*(-?\d+)", text, re.MULTILINE)
    pid_match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    pid = int(pid_match.group(1)) if pid_match else None
    # A launchctl permission/transport error is not proof the job is unloaded.
    missing = bool(re.search(r'could not find service|service not found', text, re.I))
    return {
        "loaded": True if result.returncode == 0 else False if missing else None,
        "state": state_match.group(1).strip() if state_match else "unknown",
        "last_exit_code": int(exit_match.group(1)) if exit_match else None,
        "pid": pid,
        "process_identity": liveness.process_identity(pid) if pid else None,
    }


def evaluate_health(
    runtime: dict[str, Any],
    service: dict[str, Any],
    current: datetime,
) -> dict[str, Any]:
    """Legacy pure-function compatibility ONLY; run_once never uses DB heartbeat."""
    last_exit = service.get("last_exit_code")
    if service.get("loaded") is False:
        return {"status": "failed", "error_code": "supervisor_unloaded", "age_seconds": None}
    if isinstance(last_exit, int) and last_exit != 0:
        return {"status": "failed", "error_code": "supervisor_exit_nonzero", "age_seconds": None}
    if str(runtime.get("status") or "") == "failed":
        return {"status": "failed", "error_code": "supervisor_runtime_failed", "age_seconds": None}
    heartbeat = state.parse_time(runtime.get("heartbeat_at"))
    if heartbeat is None:
        return {"status": "stale", "error_code": "supervisor_heartbeat_missing", "age_seconds": None}
    age = max(0, int((current - heartbeat).total_seconds()))
    if age > int(HEARTBEAT_GRACE.total_seconds()):
        return {"status": "stale", "error_code": "supervisor_heartbeat_stale", "age_seconds": age}
    return {"status": "healthy", "error_code": None, "age_seconds": age}


def safe_detail(health: dict[str, Any], service: dict[str, Any]) -> str:
    parts = [str(health.get("error_code") or "unknown")]
    if health.get("age_seconds") is not None:
        parts.append(f"heartbeat_age={int(health['age_seconds'])}s")
    if service.get("last_exit_code") is not None:
        parts.append(f"last_exit={int(service['last_exit_code'])}")
    return "；".join(parts)


def _open_health_alert(database):
    connection = state._connect(database, readonly=True)
    try:
        return connection.execute(
            "SELECT 1 FROM sync_alerts WHERE dedupe_key=? AND status='open'",
            ('system:supervisor_health',),
        ).fetchone() is not None
    finally:
        connection.close()


def owner_health(root, service, clock, *, open_alert=False, snapshot=None):
    """Separate sentinel fence: observations can never update the owner receipt.

    A fault/unknown/wake holds recovery until a later real pulse. With a legacy
    open alert and no fence, establish one instead of closing from old evidence.
    """
    snapshot = liveness.read(root) if snapshot is None else snapshot
    clock = clock() if callable(clock) else clock
    observation = liveness._read_json(root, liveness.OBSERVATION)
    current = dict(clock)
    if observation.get('read_status') == 'ok':
        if observation.get('schema_version') != 1 or not liveness._clock_valid(observation.get('recovery_after')):
            current['recovery_after'] = {}  # fail closed; do not overwrite corruption
        else:
            current['recovery_after'] = observation['recovery_after']
    elif observation.get('read_status') != 'missing':
        current['recovery_after'] = {}
    health = liveness.evaluate(snapshot, service, current)
    # When the clock probe itself fails, fence the last owner receipt. Recovery
    # from a transient probe failure must still require a different real pulse.
    barrier = clock if liveness._clock_valid(clock) else snapshot.get('heartbeat')
    needs_fence = health['status'] != 'healthy' or open_alert
    require_end = observation.get('require_cycle_end', False)
    if type(require_end) is not bool:
        return dict(health,status='unknown',error_code='supervisor_recovery_evidence_invalid',can_resolve=False)
    require_end = require_end or health.get('error_code') == 'supervisor_finished_receipt_required'
    if needs_fence and observation.get('read_status') == 'missing':
        if not liveness._clock_valid(barrier):
            return dict(health, status='unknown', error_code='supervisor_clock_unavailable', can_resolve=False)
        liveness._write_json(root, liveness.OBSERVATION,
                             dict(schema_version=1, recovery_after=barrier,require_cycle_end=require_end))
        current['recovery_after'] = barrier
        health = liveness.evaluate(snapshot, service, current)
    elif health['status'] not in {'healthy', 'recovery_pending'} and liveness._clock_valid(barrier):
        # Keep the last failed observation as the barrier, not the first fault:
        # a pulse observed while launchd/clock evidence was unknown cannot cure it.
        if observation.get('read_status') == 'ok' and liveness._clock_valid(observation.get('recovery_after')):
            liveness._write_json(root, liveness.OBSERVATION,
                                 dict(schema_version=1, recovery_after=barrier,require_cycle_end=require_end))
    if require_end and health.get('can_resolve'):
        terminal=snapshot.get('stage') in {'cycle_finished','cycle_deferred'} and (snapshot.get('cycle_ended') or snapshot.get('cycle_finished'))==snapshot.get('heartbeat')
        if not terminal:
            return dict(health,status='recovery_pending',error_code='supervisor_new_cycle_end_required',can_resolve=False)
        liveness._write_json(root,liveness.OBSERVATION,dict(schema_version=1,recovery_after=current['recovery_after'],require_cycle_end=False))
    return health


def run_once(
    config_path: Path = CONFIG_PATH,
    *,
    database: Path | None = None,
    current: datetime | None = None,
    maintenance_path: Path = MAINTENANCE_PATH,
    service_probe: Callable[[], dict[str, Any]] = launchd_status,
    delivery: Callable[[dict[str, Any], Path | None], int] = deliver_due_alerts,
    clock_probe: Callable[[], dict[str, Any]] = liveness.clock_sample,
) -> dict[str, Any]:
    checked = current or now_dt()
    if maintenance_active(maintenance_path):
        return {
            "status": "maintenance",
            "error_code": None,
            "heartbeat_age_seconds": None,
            "launchd_loaded": None,
            "last_exit_code": None,
            "alerts_processed": 0,
        }
    incident_root = config_path.parent
    # Non-default configurations must explicitly select a DB; never fall through
    # from an isolated root to the module's production DB constant.
    if database is None and config_path.absolute() != CONFIG_PATH.absolute():
        raise ValueError('explicit_database_required_for_custom_config')
    incidents.import_history(incident_root)
    try:
        state.initialize(database)
        incidents.register_pending(incident_root, database)
        incidents.verify_correction(incident_root, database)
        health_alert_open = _open_health_alert(database)
    except ledger_recovery.Deferred:
        return {"status":"retry_wait","error_code":"ledger_retry_wait","alerts_processed":0,"root_cause_status":"pending"}
    except (sqlite3.Error, OSError) as exc:
        incidents.retain_database_outage(incident_root, exc, Path(database or state.DATABASE))
        return {"status": "failed", "error_code": "sentinel_database_unavailable",
                "alerts_processed": 0, "notification_state": "durably_pending",
                "root_cause_status": "pending", "heartbeat_age_seconds": None,
                "launchd_loaded": None, "last_exit_code": None}
    service = service_probe()
    snapshot=liveness.read(incident_root)
    sampled_clock=clock_probe() if callable(clock_probe) else clock_probe
    health = owner_health(incident_root, service, sampled_clock, open_alert=health_alert_open,snapshot=snapshot)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config_invalid")
    if health["status"] in {"failed", "stale", "unknown"}:
        alert = state.open_alert(
            "system", "supervisor_health", "后台检查程序需要检查",
            safe_detail(health, service), severity="error", database=database, at=checked, error_code=health.get('error_code'),
        )
    else:
        alert = next((a for a in state.alerts(database) if a.get('category')=='supervisor_health' and a.get('source_id')=='system'),None)
    progress = recovery.tick(incident_root,health,service,snapshot,
                             sampled_clock,checked,alert=alert,
                             allow_request=database is None and config_path.absolute()==CONFIG_PATH.absolute(),probe=service_probe)
    if health.get('can_resolve') is True and progress.get('closed',True):
        state.resolve_alert(
            "system", "supervisor_health", title="后台检查程序已恢复",
            detail="已取得故障观察之后的新监督器主进程心跳；此证据不表示来源业务已完成。", database=database, at=checked,
        )
    if not progress.get('closed',True) and health.get('can_resolve'):
        health=dict(health,can_resolve=False,status='recovery_pending',error_code='supervisor_new_cycle_end_required')
    delivered = delivery(config, database)
    pending = incidents.unresolved(incident_root)
    return {
        "supervisor_recovery": {k:progress.get(k) for k in ('phase','request_count','next_check_at','reason','closed')},
        "status": "attention" if pending and health["status"] == "healthy" else health["status"],
        "error_code": health.get("error_code") or ("supervisor_runtime_error_pending" if pending else None),
        "heartbeat_age_seconds": health.get("age_seconds"),
        "launchd_loaded": service.get("loaded"),
        "last_exit_code": service.get("last_exit_code"),
        "alerts_processed": delivered,
        "root_cause_status": "pending" if pending else "clear",
        "unresolved_incidents": pending,
        "can_resolve": health.get("can_resolve", False),
        "owner_active": health.get("owner_active", False),
        "health_scope": "owner_liveness",
        "business_status": "not_evaluated",
        "workers": health.get("workers", []),
        "sleep_seconds": health.get("sleep_seconds"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the BUSINESS sync supervisor independently")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with sentinel_lock() as acquired:
        if not acquired:
            print(json.dumps({"status": "already_running"}, ensure_ascii=False))
            return 0
        try:
            result = run_once(args.config)
        except Exception as exc:
            code = re.sub(r"[^A-Za-z0-9_.:-]", "_", type(exc).__name__)[:80]
            print(json.dumps({"status": "failed", "error_code": code}, ensure_ascii=False))
            return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
